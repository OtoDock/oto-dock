"""A TCP relay in front of the test Postgres, in its own process, so an
outage can be staged without pausing or restarting a container the box
shares, and without relay threads competing with the measured loop.

    _pg_relay.py UPSTREAM_HOST UPSTREAM_PORT

Prints ``{"port": N}``, then obeys one word per stdin line, answering
``{"ok": word}``:

- ``cut``: a Postgres restart. The listener closes and every relayed socket
  is reset (``SO_LINGER`` 0), so live connections break and new ones are
  refused.
- ``up``: listening again on the same port.
- ``freeze``: a paused container. Every byte is held in both directions;
  new connections are still accepted (a paused container's kernel still
  completes the handshake) and hang.
- ``thaw``: the held bytes flow again.

Exits on stdin EOF, and with its parent.
"""

import contextlib
import ctypes
import json
import signal
import socket
import struct
import sys
import threading
import time


def _die_with_parent() -> None:
    with contextlib.suppress(OSError, AttributeError):
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)


class Relay:
    def __init__(self, upstream: tuple[str, int]):
        self.upstream = upstream
        self.port = 0
        self.frozen = False
        self._ls: socket.socket | None = None
        self._socks: set[socket.socket] = set()
        self._lock = threading.Lock()

    def up(self) -> None:
        ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ls.bind(("127.0.0.1", self.port))
        ls.listen(256)
        ls.settimeout(0.1)
        self.port = ls.getsockname()[1]
        self._ls = ls
        threading.Thread(target=self._accept, args=(ls,), daemon=True).start()

    def cut(self) -> None:
        ls, self._ls = self._ls, None
        if ls is not None:
            with contextlib.suppress(OSError):
                ls.close()
        with self._lock:
            socks, self._socks = list(self._socks), set()
        for s in socks:
            with contextlib.suppress(OSError):
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            with contextlib.suppress(OSError):
                s.close()

    def _accept(self, ls: socket.socket) -> None:
        while self._ls is ls:
            try:
                client, _ = ls.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            client.settimeout(None)
            try:
                up = socket.create_connection(self.upstream, timeout=5)
                up.settimeout(None)
            except OSError:
                client.close()
                continue
            with self._lock:
                self._socks.update((client, up))
            for a, b in ((client, up), (up, client)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    def _pipe(self, a: socket.socket, b: socket.socket) -> None:
        try:
            while True:
                data = a.recv(65536)
                if not data:
                    break
                while self.frozen:
                    time.sleep(0.005)
                b.sendall(data)
        except OSError:
            pass
        finally:
            for s in (a, b):
                with contextlib.suppress(OSError):
                    s.shutdown(socket.SHUT_RDWR)


def main() -> None:
    _die_with_parent()
    relay = Relay((sys.argv[1], int(sys.argv[2])))
    relay.up()
    print(json.dumps({"port": relay.port}), flush=True)
    for line in sys.stdin:
        word = line.strip()
        if word == "cut":
            relay.cut()
        elif word == "up":
            relay.up()
        elif word == "freeze":
            relay.frozen = True
        elif word == "thaw":
            relay.frozen = False
        else:
            continue
        print(json.dumps({"ok": word}), flush=True)
    relay.cut()


if __name__ == "__main__":
    main()
