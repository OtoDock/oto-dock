"""The load client: separate processes, so the load never runs on the loop
under test. Each mode reads its spec (a JSON file), prints JSON lines (one
per phase) on stdout and waits for one word on stdin between phases where
the test must act. Exits on stdin EOF, and with its parent.

    _client.py MODE SPEC_JSON

Modes: stream, pages, bodies, password, idle, download, photo.
"""

import asyncio
import contextlib
import ctypes
import json
import math
import select
import signal
import socket
import sys
import time


def _die_with_parent() -> None:
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)


def _out(**kw) -> None:
    print(json.dumps(kw), flush=True)


def _wait_word() -> None:
    if not sys.stdin.readline():
        sys.exit(0)


def _pct(values, p):
    if not values:
        return 0.0
    s = sorted(values)
    return s[max(0, math.ceil(p * len(s)) - 1)]


def _ms(seconds):
    return None if seconds is None else round(seconds * 1000, 3)


def _dist(values) -> dict:
    return {"n": len(values), "p50_ms": _ms(_pct(values, 0.5)), "p95_ms": _ms(_pct(values, 0.95)),
            "p99_ms": _ms(_pct(values, 0.99)), "max_ms": _ms(max(values, default=0.0))}


class _SelfLag:
    """This process's own loop lateness: a client that falls behind makes its
    run invalid, not the server slow."""

    def __init__(self):
        self.max = 0.0
        self._stop = False
        self._cpu0 = time.process_time()
        self._t0 = time.monotonic()

    async def run(self):
        while not self._stop:
            t = time.monotonic()
            await asyncio.sleep(0.01)
            self.max = max(self.max, time.monotonic() - t - 0.01)

    def restart(self) -> None:
        """Measure from here: the set-up before the measured phase is not
        what the check is about."""
        self.max = 0.0
        self._cpu0 = time.process_time()
        self._t0 = time.monotonic()

    def report(self) -> dict:
        self._stop = True
        wall = time.monotonic() - self._t0
        return {"lag_max_ms": _ms(self.max),
                "cpu_share": round((time.process_time() - self._cpu0) / wall, 3) if wall else None}


# --------------------------------------------------------------------------
# raw HTTP, timed from the first request byte to the response head
# --------------------------------------------------------------------------

def _head(sock, deadline) -> tuple[bytes, bytes]:
    buf = b""
    while b"\r\n\r\n" not in buf:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError("no response head")
        sock.settimeout(left)
        chunk = sock.recv(65536)
        if not chunk:
            raise ConnectionError("closed before the response head")
        buf += chunk
    head, _, rest = buf.partition(b"\r\n\r\n")
    return head, rest


def _parse_head(head: bytes) -> tuple[int, dict]:
    lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers[k.strip().lower()] = v.strip()
    return int(lines[0].split()[1]), headers


def _read_body(sock, rest: bytes, headers: dict, deadline: float) -> bytes:
    want = int(headers.get("content-length", "0") or 0)
    body = rest
    while len(body) < want and time.monotonic() < deadline:
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        chunk = sock.recv(65536)
        if not chunk:
            break
        body += chunk
    return body


def _connect(host, port, timeout=10.0) -> socket.socket:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def _request(host, port, raw: bytes, timeout=10.0) -> dict:
    sock = _connect(host, port, timeout)
    try:
        deadline = time.monotonic() + timeout
        t0 = time.perf_counter()
        sock.sendall(raw)
        head, rest = _head(sock, deadline)
        elapsed = time.perf_counter() - t0
        status, headers = _parse_head(head)
        body = _read_body(sock, rest, headers, deadline)
        return {"status": status, "headers": headers, "body": body.decode(errors="replace"),
                "elapsed": elapsed}
    finally:
        sock.close()


def _health(host, port) -> dict:
    raw = f"GET /health HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
    try:
        r = _request(host, port, raw, timeout=5.0)
        return {"status": r["status"], "ms": _ms(r["elapsed"])}
    except Exception as exc:
        return {"status": type(exc).__name__, "ms": None}


# --------------------------------------------------------------------------
# stream: real dashboard sockets, each resuming its chat
# --------------------------------------------------------------------------

async def _stream(spec: dict) -> None:
    from websockets.asyncio.client import connect

    lag = _SelfLag()
    lag_task = asyncio.create_task(lag.run())
    per_chat: dict[str, dict] = {}
    latencies: list[float] = []
    frame_types: dict[str, int] = {}

    async def open_one(conn):
        ws = await connect(spec["url"], compression="deflate", max_size=None, ping_interval=None,
                           open_timeout=60, origin=spec["origin"],
                           additional_headers={"Cookie": f"session={conn['cookie']}"})
        await ws.send(json.dumps({"type": "resume_chat", "chat_id": conn["chat"]}))
        return ws

    async def view(ws, cid):
        text, frames, histories, done_rx = [], 0, 0, None
        async for raw in ws:
            rx = time.monotonic()
            msg = json.loads(raw)
            kind = msg.get("type", "?")
            frame_types[kind] = frame_types.get(kind, 0) + 1
            if kind == "chat_history" and msg.get("chat_id") == cid:
                histories += 1
            elif kind == "text":
                frames += 1
                content = msg.get("content", "")
                text.append(content)
                for tok in content.split(";"):
                    if ",t=" in tok:
                        latencies.append(rx - float(tok.rsplit(",t=", 1)[1]))
            elif kind == "done":
                done_rx = rx
                break
        seq = [int(tok.split(",", 1)[0][2:]) for tok in "".join(text).split(";") if tok.startswith("s=")]
        per_chat[cid] = {"seq": seq, "frames": frames, "histories": histories, "done_rx": done_rx}

    socks = await asyncio.gather(*(open_one(c) for c in spec["conns"]))
    _out(phase="connected", n=len(socks))
    lag.restart()
    chats = [c["chat"] for c in spec["conns"]]
    try:
        await asyncio.wait_for(asyncio.gather(*(view(ws, cid) for ws, cid in zip(socks, chats))),
                               spec["timeout"])
    finally:
        for ws in socks:
            with contextlib.suppress(Exception):
                await ws.close()
    result = {}
    for cid in chats:
        got = per_chat.get(cid, {"seq": [], "frames": 0, "histories": 0, "done_rx": None})
        seq = got["seq"]
        result[cid] = {"received": len(seq), "in_order": seq == list(range(len(seq))),
                       "frames": got["frames"], "histories": got["histories"], "done_rx": got["done_rx"]}
    self_lag = lag.report()
    await lag_task
    _out(phase="result", chats=result, latency=_dist(latencies), frame_types=frame_types, client=self_lag)


# --------------------------------------------------------------------------
# pages: tabs of three GETs and a dashboard socket connect
# --------------------------------------------------------------------------

async def _pages(spec: dict) -> None:
    import httpx
    from websockets.asyncio.client import connect

    lag = _SelfLag()
    lag_task = asyncio.create_task(lag.run())
    agents, tabs = spec["agents"], spec["tabs"]
    routes = {
        "agents": lambda i: "/v1/agents",
        "chats": lambda i: f"/v1/chats?agent={agents[i % len(agents)]}&kind=chats&limit=50",
        "active": lambda i: "/v1/chats/active",
    }
    limits = httpx.Limits(max_connections=tabs * len(routes), max_keepalive_connections=tabs * len(routes))
    async with httpx.AsyncClient(base_url=spec["base"], headers={"Cookie": f"session={spec['cookie']}"},
                                 limits=limits, timeout=30.0) as client:

        retries = 0

        async def get(name, path):
            nonlocal retries
            t0 = time.perf_counter()
            try:
                r = await client.get(path)
            except (httpx.ReadError, httpx.RemoteProtocolError):
                # A keep-alive connection the server closed as it was reused
                # (its 2 s idle timer): a browser retries it the same way.
                retries += 1
                r = await client.get(path)
            return name, r.status_code, time.perf_counter() - t0

        async def socket_connect():
            t0 = time.perf_counter()
            try:
                async with connect(spec["ws"], compression="deflate", origin=spec["origin"],
                                   additional_headers={"Cookie": f"session={spec['cookie']}"},
                                   open_timeout=30, ping_interval=None) as ws:
                    # A tab stays open through the connect frames; server_info
                    # is the last of them.
                    async with asyncio.timeout(30):
                        while json.loads(await ws.recv()).get("type") != "server_info":
                            pass
                return "socket", 101, time.perf_counter() - t0
            except Exception:
                return "socket", 0, time.perf_counter() - t0

        async def wave():
            jobs = [get(name, make(i)) for i in range(tabs) for name, make in routes.items()]
            if spec["sockets"]:
                jobs += [socket_connect() for _ in range(tabs)]
            return await asyncio.gather(*jobs)

        await wave()
        _out(phase="warm")
        await asyncio.to_thread(_wait_word)
        lag.restart()
        per_route: dict[str, list[float]] = {name: [] for name in [*routes, *(["socket"] if spec["sockets"] else [])]}
        statuses: dict[str, dict[str, int]] = {name: {} for name in per_route}
        for _ in range(spec["rounds"]):
            for name, status, elapsed in await wave():
                per_route[name].append(elapsed)
                statuses[name][str(status)] = statuses[name].get(str(status), 0) + 1
    self_lag = lag.report()
    await lag_task
    _out(phase="result", statuses=statuses, latencies=per_route, retries=retries, client=self_lag)


# --------------------------------------------------------------------------
# bodies: a declared 8 MB body sent while the answer is awaited, then a
# chunked 100 MB one
# --------------------------------------------------------------------------

def _push_until_answer(sock, first: bytes, piece: bytes, total: int, deadline: float) -> dict:
    """Write ``first`` then ``piece`` repeatedly (up to ``total``) without
    blocking, and stop writing the moment anything is readable. Returns the
    response bytes, what was pushed and how the connection ended."""
    sock.setblocking(False)
    pending, sent, response = first, 0, b""
    status_at, eof, error = None, False, ""
    t0 = time.perf_counter()
    reading = False
    while not eof and time.monotonic() < deadline:
        want_write = not reading and not error and (pending or sent < total)
        r, w, _ = select.select([sock], [sock] if want_write else [], [], 0.5)
        if r:
            reading = True
            try:
                chunk = sock.recv(65536)
            except (BlockingIOError, InterruptedError):
                chunk = None
            except OSError as exc:
                error = error or type(exc).__name__
                break
            if chunk == b"":
                eof = True
            elif chunk:
                response += chunk
                if status_at is None and b"\r\n\r\n" in response:
                    status_at = time.perf_counter() - t0
        elif w:
            if not pending:
                pending = piece
            try:
                n = sock.send(pending)
                sent += n
                pending = pending[n:]
            except (BlockingIOError, InterruptedError):
                pass
            except OSError as exc:
                error = type(exc).__name__
    status, body, headers = None, "", {}
    if b"\r\n\r\n" in response:
        head, _, rest = response.partition(b"\r\n\r\n")
        status, headers = _parse_head(head)
        body = rest.decode(errors="replace")
    return {"status": status, "body": body, "connection": headers.get("connection", ""), "pushed": sent,
            "status_s": status_at, "closed": eof or bool(error), "reset": error}


def _bodies(spec: dict) -> None:
    _out(phase="ready")
    _wait_word()
    host, port, path = spec["host"], spec["port"], spec["path"]
    headers = (f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
               f"Content-Length: {8 * 1024 * 1024}\r\nConnection: close\r\n\r\n").encode()
    piece = b"a" * 65536
    runs = []
    for i in range(spec["tries"] + 1):
        sock = _connect(host, port)
        try:
            r = _push_until_answer(sock, headers, piece, 8 * 1024 * 1024, time.monotonic() + 10)
        finally:
            sock.close()
        if i:
            runs.append(r)
    _out(phase="declared", statuses=sorted({str(r["status"]) for r in runs}),
         bodies=sorted({r["body"] for r in runs}), connection=sorted({r["connection"] for r in runs}),
         timing=_dist([r["status_s"] for r in runs if r["status_s"] is not None]),
         pushed_max=max(r["pushed"] for r in runs))
    sock = _connect(host, port)
    chunk = f"{len(piece):x}\r\n".encode() + piece + b"\r\n"
    try:
        r = _push_until_answer(
            sock,
            (f"POST {path} HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
             "Transfer-Encoding: chunked\r\n\r\n").encode(),
            chunk, 100 * 1024 * 1024, time.monotonic() + 30)
    finally:
        sock.close()
    _out(phase="chunked", status=r["status"], body=r["body"], pushed=r["pushed"], closed=r["closed"],
         reset=r["reset"], status_ms=_ms(r["status_s"]))


def _password(spec: dict) -> None:
    _out(phase="ready")
    _wait_word()
    host, port = spec["host"], spec["port"]
    body = json.dumps({"token": spec["token"], "new_password": "a" * 700}).encode()
    raw = (f"POST /auth/reset-password HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
           f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n").encode() + body
    runs = [_request(host, port, raw) for _ in range(spec["tries"] + 1)][1:]
    details = sorted({json.loads(r["body"]).get("detail", "") for r in runs if r["body"]})
    _out(phase="result", statuses=sorted({r["status"] for r in runs}), details=details,
         timing=_dist([r["elapsed"] for r in runs]))


# --------------------------------------------------------------------------
# idle: sockets that never send a byte
# --------------------------------------------------------------------------

def _idle(spec: dict) -> None:
    _out(phase="ready")
    _wait_word()
    host, port, count = spec["host"], spec["port"], spec["count"]
    socks, opened_at = [], []
    for _ in range(count):
        try:
            socks.append(socket.create_connection((host, port), timeout=5))
            opened_at.append(time.monotonic())
        except OSError as exc:
            _out(phase="open", opened=len(socks), error=type(exc).__name__)
            break
    else:
        _out(phase="open", opened=len(socks))
    _wait_word()
    _out(phase="health", health=[_health(host, port) for _ in range(3)])
    _wait_word()
    open_socks = {s.fileno(): (s, t) for s, t in zip(socks, opened_at)}
    for s in socks:
        s.setblocking(False)
    closed_after: list[float] = []
    deadline = time.monotonic() + spec["wait_s"]
    poller = select.epoll()
    for fd in open_socks:
        poller.register(fd, select.EPOLLIN | select.EPOLLRDHUP | select.EPOLLHUP)
    while open_socks and time.monotonic() < deadline:
        for fd, _ev in poller.poll(0.5):
            s, t = open_socks.pop(fd)
            closed_after.append(time.monotonic() - t)
            poller.unregister(fd)
    _out(phase="after", closed_by_server=len(closed_after), still_open=len(open_socks),
         closed_after_min_s=round(min(closed_after), 2) if closed_after else None,
         closed_after_max_s=round(max(closed_after), 2) if closed_after else None,
         health=[_health(host, port) for _ in range(3)])
    for s in socks:
        s.close()


# --------------------------------------------------------------------------
# download and photo
# --------------------------------------------------------------------------

def _download(spec: dict) -> None:
    host, port = spec["host"], spec["port"]
    runs = []
    _out(phase="ready")
    for path in spec["paths"]:
        _wait_word()
        sock = _connect(host, port, timeout=60)
        try:
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            deadline = time.monotonic() + 120
            head, rest = _head(sock, deadline)
            status = _parse_head(head)[0]
            got = len(rest)
            t0 = time.perf_counter()
            while time.monotonic() < deadline:
                chunk = sock.recv(1 << 20)
                if not chunk:
                    break
                got += len(chunk)
            runs.append({"status": status, "bytes": got, "s": round(time.perf_counter() - t0, 3)})
        finally:
            sock.close()
        _out(phase="downloaded", **runs[-1])


async def _photo(spec: dict) -> None:
    from websockets.asyncio.client import connect

    frames = []
    for path in spec["frame_files"]:
        with open(path, encoding="ascii") as fh:
            frames.append(fh.read())
    _out(phase="ready")
    for frame in frames:
        await asyncio.to_thread(_wait_word)
        async with connect(spec["url"], compression="deflate", max_size=None, ping_interval=None,
                           open_timeout=30) as ws:
            t0 = time.perf_counter()
            await ws.send(frame)
            answer = json.loads(await asyncio.wait_for(ws.recv(), 60))
            _out(phase="saved", s=round(time.perf_counter() - t0, 3), **answer)


def main() -> None:
    _die_with_parent()
    mode = sys.argv[1]
    with open(sys.argv[2], encoding="utf-8") as fh:
        spec = json.load(fh)
    if mode == "stream":
        asyncio.run(_stream(spec))
    elif mode == "pages":
        asyncio.run(_pages(spec))
    elif mode == "bodies":
        _bodies(spec)
    elif mode == "password":
        _password(spec)
    elif mode == "idle":
        _idle(spec)
    elif mode == "download":
        _download(spec)
    elif mode == "photo":
        asyncio.run(_photo(spec))
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
