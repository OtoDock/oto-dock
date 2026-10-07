"""The stream feeder: a separate process that plays N Claude CLI turns into N
pipes, one stream-json line at a time, so the proxy's loop pays the pipe
reads and the parsing the way it does for real sessions.

    _feeder.py RATE DELTAS STAGGER_S FD[,FD...]

Prints ``{"phase": "ready"}`` and waits for ``go`` on stdin. Then, per pipe,
starting ``i * STAGGER_S / N`` seconds after ``go``: ``message_start``, a text
``content_block_start``, DELTAS text deltas ``s=<k>,t=<t>;`` at RATE per second
(``t`` is when the line was due, on the monotonic clock), ``content_block_stop``,
``message_delta``, ``message_stop``, the ``assistant`` message and a ``result``
with usage and cost carrying ``t_end``. Writes never block: a pipe whose
reader lags keeps its lines in a backlog, and the other pipes keep their
schedule. Ends with ``{"phase": "fed", ...}`` and closes every pipe. Exits on
stdin EOF, and with its parent.
"""

import contextlib
import ctypes
import fcntl
import heapq
import json
import os
import signal
import sys
import time


def _die_with_parent() -> None:
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)


def _dump(obj: dict) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode()


def _event(sid: str, k: int, event: dict) -> bytes:
    return _dump({"type": "stream_event", "event": event, "session_id": sid,
                  "parent_tool_use_id": None,
                  "uuid": f"{k:08x}-4a1b-4c2d-8e3f-{os.getpid():012x}"})


_USAGE = {"input_tokens": 3, "cache_creation_input_tokens": 1800, "cache_read_input_tokens": 24000,
          "output_tokens": 0, "service_tier": "standard"}


def _opening(sid: str) -> bytes:
    msg = {"model": "claude-sonnet-5", "id": f"msg_{sid}", "type": "message", "role": "assistant",
           "content": [], "stop_reason": None, "stop_sequence": None, "usage": dict(_USAGE, output_tokens=1)}
    return (_event(sid, 0, {"type": "message_start", "message": msg})
            + _event(sid, 0, {"type": "content_block_start", "index": 0,
                              "content_block": {"type": "text", "text": ""}}))


def _closing(sid: str, deltas: int, text: str) -> bytes:
    usage = dict(_USAGE, output_tokens=deltas)
    assistant = {"type": "assistant", "session_id": sid, "parent_tool_use_id": None,
                 "message": {"model": "claude-sonnet-5", "id": f"msg_{sid}", "type": "message",
                             "role": "assistant", "content": [{"type": "text", "text": text}],
                             "stop_reason": "end_turn", "usage": usage}}
    return (_event(sid, deltas, {"type": "content_block_stop", "index": 0})
            + _event(sid, deltas, {"type": "message_delta",
                                   "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                   "usage": usage})
            + _event(sid, deltas, {"type": "message_stop"})
            + _dump(assistant))


def _result(sid: str, deltas: int, rate: float) -> bytes:
    return _dump({"type": "result", "subtype": "success", "is_error": False,
                  "duration_ms": int(deltas / rate * 1000), "duration_api_ms": int(deltas / rate * 1000),
                  "num_turns": 1, "result": "", "session_id": sid, "total_cost_usd": 0.0123,
                  "usage": dict(_USAGE, output_tokens=deltas),
                  "modelUsage": {"claude-sonnet-5": {"inputTokens": 3, "outputTokens": deltas,
                                                     "cacheReadInputTokens": 24000,
                                                     "cacheCreationInputTokens": 1800,
                                                     "costUSD": 0.0123, "contextWindow": 200000}},
                  "t_end": time.monotonic()})


def main() -> None:
    _die_with_parent()
    rate, deltas, stagger = float(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
    fds = [int(x) for x in sys.argv[4].split(",")]
    for fd in fds:
        fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)
    n = len(fds)
    print(json.dumps({"phase": "ready", "pipes": n}), flush=True)
    if sys.stdin.readline().strip() != "go":
        return
    t0 = time.monotonic()
    backlog = [b""] * n
    closing = [False] * n
    texts: list[list[str]] = [[] for _ in range(n)]
    first_at = [0.0] * n
    last_at = [0.0] * n
    backlog_max = 0
    # (due, pipe, step): -1 = the opening lines, 0..DELTAS-1 = a delta,
    # DELTAS = the closing lines and the result.
    due = [(t0 + i * stagger / n, i, -1) for i in range(n)]
    heapq.heapify(due)
    late_max = 0.0

    def flush(i: int) -> None:
        nonlocal backlog_max
        if backlog[i]:
            try:
                written = os.write(fds[i], backlog[i])
                backlog[i] = backlog[i][written:]
            except BlockingIOError:
                pass
            backlog_max = max(backlog_max, len(backlog[i]))
        if closing[i] and not backlog[i] and fds[i] >= 0:
            os.close(fds[i])
            fds[i] = -1

    while due or any(backlog):
        if due:
            at, i, step = due[0]
            now = time.monotonic()
            if at > now:
                for j in range(n):
                    flush(j)
                time.sleep(min(at - now, 0.002))
                continue
            heapq.heappop(due)
            late_max = max(late_max, now - at)
            sid = f"lt-session-{i}"
            if step == -1:
                backlog[i] += _opening(sid)
                heapq.heappush(due, (at, i, 0))
            elif step < deltas:
                text = f"s={step},t={at:.6f};"
                texts[i].append(text)
                backlog[i] += _event(sid, step, {"type": "content_block_delta", "index": 0,
                                                 "delta": {"type": "text_delta", "text": text}})
                if step == 0:
                    first_at[i] = now
                last_at[i] = now
                heapq.heappush(due, (at + 1 / rate, i, step + 1))
            else:
                backlog[i] += _closing(sid, deltas, "".join(texts[i])) + _result(sid, deltas, rate)
                texts[i] = []
                closing[i] = True
            flush(i)
        else:
            for j in range(n):
                flush(j)
            time.sleep(0.002)
    spans = [last_at[i] - first_at[i] for i in range(n)]
    expected = (deltas - 1) / rate
    print(json.dumps({"phase": "fed", "late_max_ms": round(late_max * 1000, 2),
                      "backlog_max_bytes": backlog_max,
                      "rate_kept_min": round(expected / max(spans), 3) if max(spans) > 0 else None}),
          flush=True)


if __name__ == "__main__":
    main()
