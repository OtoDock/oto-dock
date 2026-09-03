"""Isolated document-work workers (parse, render, write).

A job runs in a fresh spawn child under RLIMIT_AS with a wall-clock
deadline: a pathological document costs one bounded child process and comes
back as a clean per-call error — never a ballooned shared server (one
unbounded read_document took a 12GB host down with the whole install on it).

Budgets derive from the container's own cgroup limit so the same code fits a
2g T1 sidecar and a 4g T2 service: workers are sized to die at THEIR limit
before the cgroup OOM killer can pick off the server process.

Worker targets must be module-level, picklable, pure functions over
pre-resolved container-absolute paths and plain data. They must never shell
out — RLIMIT_AS survives fork+exec, so a child-spawned soffice would inherit
the cap and fail confusingly. They must never touch the session ContextVars
or the proxy HTTP API — path resolution and preview pushes are parent-side.
Targets that write user-visible files must save to a same-directory temp
file and ``os.replace`` — children are routinely killed (RLIMIT_AS,
deadline, daemon teardown) and an in-place save killed mid-flush truncates
the user's existing file.

Results may be any picklable value (strings, or JSON-ish structures carrying
``bytes``). Strings truncate at 512KB child-side; structured results are
capped at a 64MB pickled frame (backstop for the pipe and the parent, not a
tuning knob).

``FILETOOLS_ISOLATION_INLINE=1`` runs targets in-process (in the default
executor, under the same semaphore, with the same string caps and the same
exception→message mapping, but no rlimit and no deadline). It keeps the test
suite fast and doubles as an operator escape hatch if spawn misbehaves on
some platform.
"""

import asyncio
import functools
import logging
import multiprocessing
import os
import pickle
import time
import weakref
from pathlib import Path

logger = logging.getLogger(__name__)

_GIB = 1024 ** 3
# Server-process share of the cgroup: idle ~110MB, plus warm rembg/onnxruntime
# (up to ~1GB after the first background removal) and transient soffice runs.
_HEADROOM_BYTES = int(1.2 * _GIB)
# A spawn child maps ~650MB of shared libs/arenas before parsing a byte —
# address space that RLIMIT_AS counts but that isn't real parse budget.
_AS_BASELINE_BYTES = 700 * 1024 ** 2
_MIN_BUDGET_BYTES = 512 * 1024 ** 2
_UNLIMITED_BUDGET_BYTES = 2 * _GIB
_RESULT_CAP_CHARS = 512 * 1024
_RESULT_CAP_STRUCT_BYTES = 64 * 1024 ** 2

# Default advice tails — parse-flavoured, because the parse paths were the
# original (and remain the most common) callers. Render/write call sites pass
# ``_advice`` to replace them with tool-appropriate guidance.
_DEFAULT_TOO_LARGE_ADVICE = (
    "Read a part of it instead: for PDFs pass a `pages` range (e.g. "
    "\"1-20\"), for XLSX pass `start_cell`/`end_cell` or export a smaller "
    "range"
)
_DEFAULT_TIMEOUT_ADVICE = (
    "Read a part of the document instead: `pages` ranges for PDFs, "
    "`start_cell`/`end_cell` for XLSX"
)
_DEFAULT_TRUNCATION_ADVICE = (
    "Read the part you need instead: the `pages` parameter for PDFs (e.g. "
    "\"120-140\"), `start_cell`/`end_cell` for XLSX"
)

_TRUNCATION_NOTE = f"\n\n(Output truncated at 512KB. {_DEFAULT_TRUNCATION_ADVICE}.)"


def _truncation_note(advice: str | None) -> str:
    if advice is None:
        return _TRUNCATION_NOTE
    return f"\n\n(Output truncated at 512KB. {advice}.)"


def _cgroup_limit_bytes() -> int | None:
    """This container's memory limit; None when unlimited/undetectable."""
    for p in (
        "/sys/fs/cgroup/memory.max",  # cgroup v2
        "/sys/fs/cgroup/memory/memory.limit_in_bytes",  # cgroup v1
    ):
        try:
            raw = Path(p).read_text().strip()
        except OSError:
            continue
        if raw == "max":
            return None
        try:
            value = int(raw)
        except ValueError:
            continue
        # v1 reports "no limit" as a huge page-rounded number.
        if value <= 0 or value >= 1 << 60:
            return None
        return value
    return None


@functools.lru_cache(maxsize=1)
def parse_permits() -> int:
    """Concurrent workers. One on small cgroups so permits × budget
    + headroom stays within the limit."""
    limit = _cgroup_limit_bytes()
    return 2 if limit is None or limit >= 3 * _GIB else 1


@functools.lru_cache(maxsize=1)
def worker_rss_budget_bytes() -> int:
    """Per-worker memory budget (the number quoted in error messages)."""
    env = os.environ.get("FILETOOLS_PARSE_MEM_MB", "").strip()
    if env:
        try:
            return max(256, int(env)) * 1024 ** 2
        except ValueError:
            logger.warning("Ignoring non-numeric FILETOOLS_PARSE_MEM_MB=%r", env)
    limit = _cgroup_limit_bytes()
    if limit is None:
        return _UNLIMITED_BUDGET_BYTES
    return max(_MIN_BUDGET_BYTES, (limit - _HEADROOM_BYTES) // parse_permits())


def _default_timeout_s() -> float:
    try:
        return float(os.environ.get("FILETOOLS_PARSE_TIMEOUT_S", "300"))
    except ValueError:
        return 300.0


def _inline_enabled() -> bool:
    # Read per call, never at import — tests toggle it per case.
    return os.environ.get("FILETOOLS_ISOLATION_INLINE", "").strip().lower() in (
        "1", "true", "yes",
    )


def _too_large_message(budget_bytes: int, advice: str | None = None) -> str:
    return (
        f"document is too large or complex to process (exceeded the "
        f"{budget_bytes // (1024 ** 2)}MB parse-worker memory limit). "
        f"{advice or _DEFAULT_TOO_LARGE_ADVICE}"
    )


def _cap_str_result(out, advice: str | None):
    """Apply the string cap; non-strings pass through untouched."""
    if isinstance(out, str) and len(out) > _RESULT_CAP_CHARS:
        return out[:_RESULT_CAP_CHARS] + _truncation_note(advice)
    return out


def _looks_like_oom(exc: BaseException) -> bool:
    """C extensions surface allocation failure at the rlimit as library
    errors, not MemoryError — mupdf raises RuntimeError("code=2: malloc
    (N bytes) failed") — and those must still carry the actionable advice
    instead of a raw malloc message."""
    if isinstance(exc, MemoryError):
        return True
    text = str(exc).lower()
    return (
        ("malloc" in text and "failed" in text)
        or "cannot allocate memory" in text
        or "out of memory" in text
    )


def _child_main(conn, budget_bytes: int, target, args: tuple, kwargs: dict,
                advice: str | None) -> None:
    """Runs in the spawn child: cap the address space, run the job, ship a
    pickled (status, payload) frame back. Errors travel as strings only —
    exception objects may not unpickle in the parent."""
    def _send(status: str, payload) -> None:
        conn.send_bytes(
            pickle.dumps((status, payload), protocol=pickle.HIGHEST_PROTOCOL)
        )

    try:
        import resource

        cap = budget_bytes + _AS_BASELINE_BYTES
        resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        out = _cap_str_result(target(*args, **kwargs), advice)
        frame = pickle.dumps(("ok", out), protocol=pickle.HIGHEST_PROTOCOL)
        if not isinstance(out, str) and len(frame) > _RESULT_CAP_STRUCT_BYTES:
            _send(
                "error",
                f"result too large to return "
                f"({len(frame) // (1024 ** 2)}MB serialized). "
                f"{advice or _DEFAULT_TOO_LARGE_ADVICE}",
            )
        else:
            conn.send_bytes(frame)
    except BaseException as exc:  # noqa: BLE001 — everything becomes a message
        try:
            if _looks_like_oom(exc):
                _send("error", _too_large_message(budget_bytes, advice))
            else:
                _send("error", str(exc) or exc.__class__.__name__)
        except Exception:
            pass
    finally:
        conn.close()


def _recv_with_deadline(conn, proc, deadline_s: float) -> tuple:
    """Blocking (runs in an executor thread): wait for the child's frame.

    recv BEFORE join, always — a child sending a large result blocks in
    send_bytes() until we read, so joining first would deadlock against a
    healthy child. Returns ("timeout", None) past the deadline and
    ("died", None) when the child vanished without a full frame (SIGKILL by
    the cgroup killer, a C-extension abort on allocation failure, or a pipe
    closed mid-frame — CPython raises OSError("got end of file during
    message") for a partial length-prefixed frame, and 64MB structured
    frames make that window far wider than the old 512KB strings)."""
    end = time.monotonic() + deadline_s
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            return ("timeout", None)
        if conn.poll(min(remaining, 0.5)):
            try:
                return pickle.loads(conn.recv_bytes())
            except (EOFError, OSError):
                return ("died", None)
        if not proc.is_alive() and not conn.poll(0):
            return ("died", None)


# One semaphore per event loop (the server has one loop; tests create fresh
# loops per case and must not inherit a semaphore bound to a dead loop).
_sems: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


def _get_sem() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _sems.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(parse_permits())
        _sems[loop] = sem
    return sem


async def run_parse(target, /, *args, _timeout_s: float | None = None,
                    _advice: str | None = None, **kwargs):
    """Run ``target(*args, **kwargs)`` in a bounded, deadline-limited child.

    Returns the child's result (any picklable value; strings truncate at
    512KB); raises RuntimeError with a self-contained, user-facing message on
    over-budget, timeout, abnormal child death, or any in-child exception.

    ``_timeout_s`` and ``_advice`` are reserved control parameters (never
    forwarded to ``target``): a per-call deadline override, and the
    "what to do instead" tail used in over-budget/timeout/truncation
    messages (defaults keep the parse-flavoured wording).
    """
    budget = worker_rss_budget_bytes()
    deadline = _timeout_s if _timeout_s is not None else _default_timeout_s()
    loop = asyncio.get_running_loop()

    if _inline_enabled():
        # In-process fallback: same semaphore, same caps, same
        # exception→message mapping — but no rlimit and no deadline. Runs in
        # the executor so a long job never blocks the event loop. Only
        # Exception is mapped: BaseException (CancelledError,
        # KeyboardInterrupt) must propagate in-process.
        async with _get_sem():
            try:
                out = await loop.run_in_executor(
                    None, functools.partial(target, *args, **kwargs)
                )
            except Exception as exc:
                if _looks_like_oom(exc):
                    raise RuntimeError(_too_large_message(budget, _advice)) from None
                raise RuntimeError(str(exc) or exc.__class__.__name__) from exc
        return _cap_str_result(out, _advice)

    ctx = multiprocessing.get_context("spawn")
    async with _get_sem():
        parent_conn, child_conn = ctx.Pipe(duplex=False)
        # daemon: the interpreter terminates surviving children at exit, so a
        # SIGTERM'd server never leaves an orphan mid-job.
        proc = ctx.Process(
            target=_child_main,
            args=(child_conn, budget, target, args, kwargs, _advice),
            daemon=True,
        )
        proc.start()
        child_conn.close()  # our copy; the child holds the write end
        try:
            status, payload = await loop.run_in_executor(
                None, _recv_with_deadline, parent_conn, proc, deadline
            )
        finally:
            if proc.is_alive():
                proc.terminate()
                proc.join(5)
                if proc.is_alive():
                    proc.kill()
                    proc.join(5)
            else:
                proc.join(5)
            parent_conn.close()

    if status == "ok":
        return payload
    if status == "error":
        raise RuntimeError(payload)
    if status == "timeout":
        raise RuntimeError(
            f"processing took longer than {deadline:.0f}s and was cancelled. "
            f"{_advice or _DEFAULT_TIMEOUT_ADVICE}"
        )
    # "died" — killed without a result.
    raise RuntimeError(_too_large_message(budget, _advice) + " (the parse worker was terminated)")
