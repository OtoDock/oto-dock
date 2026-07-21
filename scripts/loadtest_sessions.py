#!/usr/bin/env python3
"""Concurrency load-test + memory/CPU calibration harness.

Calibrates the per-session reservation estimates used by the live-RAM admission
gates (``SESSION_EST_HEAVY_MB`` / ``SESSION_EST_LIGHT_MB`` in ``proxy/config.py``,
read by ``proxy/core/host_resources.py``) by spawning K concurrent agent sessions
through the REAL dashboard WebSocket and measuring the proxy process tree's RSS +
CPU at each step. Run it on an otherwise-idle proxy for a clean baseline.

  WHAT IT MEASURES
    A locally-run session is a `claude`/`codex` + MCP subprocess tree spawned as
    a child of the proxy. So per-session cost = (proxy-tree RSS at K sessions −
    baseline) / K. We sample the proxy PID and all its descendants from /proc
    (dependency-free) plus the cgroup's memory.current when present.

  AUTH (pick one)
    --session-cookie <jwt>   Paste the `session` cookie from an authenticated
                             browser (DevTools → Application → Cookies). Simplest;
                             needs no proxy imports.
    --mint-as <email>        Mint a session JWT for this user by importing the
                             proxy's auth module. Must run in the proxy venv on
                             the proxy host. Convenient for automation.

  LOCAL CALIBRATION (the common case — run on the proxy host)
    ./venv/bin/python ../scripts/loadtest_sessions.py \
        --agent my-heavy-agent --mint-as admin@example.com \
        --steps 1,2,4,8,16

  SATELLITE CALIBRATION
    Point --agent at an agent whose default execution layer is the satellite,
    and run a SAMPLER on the satellite host to watch the agent process tree there
    (the CLIs run on the satellite, not the proxy):
        python3 scripts/loadtest_sessions.py --sample-only --proxy-match claude
    ...while driving load from anywhere:
        python3 scripts/loadtest_sessions.py --drive-only --agent <remote-agent> ...

Output: a per-step table (K, total RSS, Δ/session, cgroup mem, CPU%) and a
suggested per-session estimate (SESSION_EST_HEAVY_MB for a CLI/Codex agent,
SESSION_EST_LIGHT_MB for a Direct-LLM agent). Nothing here mutates the platform;
it only opens WS chats (which idle-reap normally) and reads /proc + /sys.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
import uuid
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
_PROXY = _REPO / "proxy"

# ---------------------------------------------------------------------------
# Measurement (dependency-free /proc + /sys readers)
# ---------------------------------------------------------------------------

_CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _num_cpus() -> int:
    try:
        return max(1, len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return max(1, os.cpu_count() or 1)


def _all_pids() -> list[int]:
    return [int(p) for p in os.listdir("/proc") if p.isdigit()]


def _stat_fields(pid: int) -> list[str] | None:
    # /proc/<pid>/stat: comm is in parens and may contain spaces — split on the
    # last ')' so ppid/utime/stime fields are positional-correct.
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
    except OSError:
        return None
    rp = data.rfind(")")
    if rp < 0:
        return None
    return ["", data[:rp]] + data[rp + 2:].split()


def _ppid(pid: int) -> int | None:
    f = _stat_fields(pid)
    return int(f[3]) if f else None  # field 4 (1-indexed) = ppid


def _rss_bytes(pid: int) -> int:
    f = _stat_fields(pid)
    try:
        return int(f[23]) * _PAGE if f else 0  # field 24 = rss (pages)
    except (ValueError, IndexError):
        return 0


def _cpu_jiffies(pid: int) -> int:
    f = _stat_fields(pid)
    try:
        return int(f[13]) + int(f[14]) if f else 0  # utime + stime
    except (ValueError, IndexError):
        return 0


def _descendants(root: int) -> list[int]:
    """root + all transitive children, from a single /proc ppid snapshot."""
    children: dict[int, list[int]] = {}
    for pid in _all_pids():
        pp = _ppid(pid)
        if pp is not None:
            children.setdefault(pp, []).append(pid)
    out, stack = [], [root]
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def measure_tree(root: int) -> tuple[int, int, int]:
    """Return (total_rss_bytes, total_cpu_jiffies, process_count) for the tree."""
    rss = jif = n = 0
    for pid in _descendants(root):
        r = _rss_bytes(pid)
        if r:
            rss += r
            jif += _cpu_jiffies(pid)
            n += 1
    return rss, jif, n


def read_cgroup_current() -> int | None:
    for path in ("/sys/fs/cgroup/memory.current",
                 "/sys/fs/cgroup/memory/memory.usage_in_bytes"):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            continue
    return None


def find_proxy_pid(match: str) -> int | None:
    """First PID whose cmdline contains `match` (and isn't this script)."""
    me = os.getpid()
    for pid in _all_pids():
        if pid == me:
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if match in cmd and "loadtest_sessions" not in cmd:
            return pid
    return None


def _mib(b: int | None) -> str:
    return f"{b / 1024 / 1024:7.0f}" if b else "      ?"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

def resolve_cookie(args) -> str:
    if args.session_cookie:
        return args.session_cookie
    if not args.mint_as:
        sys.exit("auth required: pass --session-cookie <jwt> or --mint-as <email>")
    # Mint by importing the proxy's auth module (must run in the proxy venv).
    sys.path.insert(0, str(_PROXY))
    try:
        from storage import database as task_store
        from auth.providers import create_session_jwt
    except Exception as e:  # noqa: BLE001
        sys.exit(f"--mint-as needs the proxy venv + DB ({e}); use --session-cookie instead")
    user = task_store.get_user_by_email(args.mint_as) if hasattr(task_store, "get_user_by_email") else None
    if not user:
        # Fall back: scan users for the email (schema-agnostic).
        for u in (task_store.list_users() if hasattr(task_store, "list_users") else []):
            if u.get("email") == args.mint_as:
                user = u
                break
    if not user:
        sys.exit(f"no user found for {args.mint_as}")
    return create_session_jwt(user["sub"], user["email"], user.get("name", "loadtest"), user["role"])


# ---------------------------------------------------------------------------
# WS load driver — one persistent connection == one warmed chat
# ---------------------------------------------------------------------------

async def drive_session(idx: int, args, cookie: str, stop: asyncio.Event) -> None:
    """Open a dashboard WS, warm a NEW chat on the agent, send one big prompt,
    then idle (draining frames) until `stop`. Mirrors the real client sequence:
    client_info → warmup(chat_id) → chat(text)."""
    import websockets  # local import so --sample-only needs no ws lib

    chat_id = str(uuid.uuid4())
    headers = [("Cookie", f"session={cookie}")]
    try:
        async with websockets.connect(
            args.proxy_url, additional_headers=headers, max_size=None,
            open_timeout=30, ping_interval=20,
        ) as ws:
            await ws.send(json.dumps({"type": "client_info", "platform": "loadtest",
                                      "time_zone": "UTC"}))
            warmup = {
                "type": "warmup", "agent": args.agent, "chat_id": chat_id,
                "permission_mode": "dontAsk",  # don't block the turn on a perm prompt
                "model": args.model, "execution_path": args.execution_path,
                # The first prompt rides WITH warmup — the real client behavior
                # (useDashboardWs.ts): the backend persists it + server-kicks the
                # turn on spawn. A separate `chat` send does NOT trigger the
                # new-chat spawn, so a text-less warmup never starts a session.
                "text": args.prompt,
            }
            if args.interactive:
                warmup["execution_mode"] = "interactive"  # PTY TUI instead of headless -p
            await ws.send(json.dumps(warmup))
            # warmup-with-text spawns the session + runs the first turn; just keep
            # the connection open and drain frames until the ramp says stop.
            while not stop.is_set():
                try:
                    await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except Exception:  # noqa: BLE001
                    break
    except Exception as e:  # noqa: BLE001
        print(f"  [session {idx}] driver error: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run(args) -> None:
    proxy_pid = args.proxy_pid or find_proxy_pid(args.proxy_match)
    if not proxy_pid:
        sys.exit(f"could not find proxy PID (cmdline match {args.proxy_match!r}); pass --proxy-pid")
    ncpu = _num_cpus()
    print(f"proxy pid={proxy_pid}  cpus={ncpu}  clk_tck={_CLK_TCK}")

    def sample() -> tuple[int, int, int]:
        return measure_tree(proxy_pid)

    if args.sample_only:
        print("sample-only: Ctrl-C to stop")
        prev_j, prev_t = sample()[1], time.monotonic()
        while True:
            await asyncio.sleep(args.settle)
            rss, jif, n = sample()
            now = time.monotonic()
            cpu = 100.0 * (jif - prev_j) / _CLK_TCK / max(1e-6, now - prev_t)
            prev_j, prev_t = jif, now
            print(f"  rss={_mib(rss)} MiB  procs={n:3d}  cgroup={_mib(read_cgroup_current())} MiB  cpu={cpu:5.0f}%")

    cookie = resolve_cookie(args)
    steps = [int(s) for s in args.steps.split(",") if s.strip()]
    print(f"agent={args.agent}  steps={steps}  settle={args.settle}s\n")

    base_rss, base_j, base_n = sample()
    print(f"baseline: rss={_mib(base_rss)} MiB  procs={base_n}\n")
    print(f"{'K':>3} {'totalRSS':>9} {'Δ/sess':>8} {'cgroup':>8} {'cpu%':>6} {'procs':>6}")

    drivers: list[asyncio.Task] = []
    stop = asyncio.Event()
    rows = []
    try:
        for k in steps:
            while len(drivers) < k:
                i = len(drivers)
                drivers.append(asyncio.create_task(drive_session(i, args, cookie, stop)))
            # Let the new sessions warm + run their first turn, then settle.
            j0, t0 = sample()[1], time.monotonic()
            await asyncio.sleep(args.settle)
            rss, jif, n = sample()
            cpu = 100.0 * (jif - j0) / _CLK_TCK / max(1e-6, time.monotonic() - t0)
            per = (rss - base_rss) / max(1, k)
            rows.append((k, rss, per, cpu))
            print(f"{k:>3} {_mib(rss)} {_mib(int(per))} {_mib(read_cgroup_current())} {cpu:6.0f} {n:>6}")
    finally:
        stop.set()
        for d in drivers:
            d.cancel()
        await asyncio.gather(*drivers, return_exceptions=True)

    if rows:
        # Per-session estimate = median Δ/session across steps with ≥2 sessions
        # (the 1-session step over-counts shared/first-touch memory).
        per_vals = sorted(p for k, _, p, _ in rows if k >= 2) or [rows[-1][2]]
        per_mb = per_vals[len(per_vals) // 2] / 1024 / 1024
        peak_cpu_per = max((c / k for k, _, _, c in rows), default=0)
        print("\n=== suggested constants (proxy/config.py) ===")
        print(f"  SESSION_EST_HEAVY_MB = {per_mb:.0f}    # measured Δ RSS per CLI/Codex session")
        print(f"  SESSION_EST_LIGHT_MB = 350    # Direct-LLM (no CLI process); measure separately with a direct-llm agent")
        if peak_cpu_per > 0:
            print(f"  (~{peak_cpu_per:.0f}% CPU/session at peak — informational; admission is "
                  f"memory-gated, CPU is not a gate)")
        print("  (re-run on the target box size / satellite to recalibrate)")


def main() -> None:
    ap = argparse.ArgumentParser(description="OtoDock concurrency load-test + sizing calibration")
    ap.add_argument("--agent", help="agent slug to spawn sessions on (a representative multi-MCP agent)")
    ap.add_argument("--steps", default="1,2,4,8,16", help="comma-separated concurrency steps")
    ap.add_argument("--settle", type=float, default=60.0,
                    help="seconds to wait at each step before sampling — MUST exceed cold MCP "
                         "install/spawn time (30-60s+) or the session tree won't be up yet. "
                         "NOTE: only LOCAL sessions grow the proxy tree; if the agent routes to a "
                         "satellite, run with --sample-only ON that satellite instead.")
    ap.add_argument("--proxy-url", default=f"ws://127.0.0.1:{os.environ.get('PROXY_PORT', '8400')}/ws/dashboard")
    ap.add_argument("--proxy-pid", type=int, default=0, help="proxy PID (else auto-detect via --proxy-match)")
    ap.add_argument("--proxy-match", default="uvicorn", help="cmdline substring to find the proxy PID")
    ap.add_argument("--model", default="", help="model override (blank = agent default)")
    ap.add_argument("--execution-path", default="", help="claude-code-cli | codex-cli (blank = agent default)")
    ap.add_argument("--interactive", action="store_true", help="spawn interactive PTY (TUI) sessions instead of headless -p (heavier per-session)")
    ap.add_argument("--prompt", default="List the files in the current directory and summarize what this project does in 3 sentences.")
    ap.add_argument("--session-cookie", default="", help="session JWT from an authenticated browser")
    ap.add_argument("--mint-as", default="", help="email to mint a session JWT for (needs proxy venv + DB)")
    ap.add_argument("--sample-only", action="store_true", help="just sample the proxy tree (no load); for satellite hosts")
    ap.add_argument("--drive-only", action="store_true", help="(reserved) only drive load; sampling done elsewhere")
    args = ap.parse_args()
    if not args.sample_only and not args.agent:
        ap.error("--agent is required unless --sample-only")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
