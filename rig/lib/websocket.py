"""The smallest RFC 6455 client the rig needs, on the standard library: text
frames over one plain or TLS socket, a ping answered, a close honoured. The rig
runs on a bare ``python3`` on any machine, so it carries no package."""
from __future__ import annotations

import base64
import contextlib
import json
import os
import select
import socket
import ssl
import struct
from urllib.parse import urlparse

_OP_TEXT, _OP_BIN, _OP_CLOSE, _OP_PING, _OP_PONG, _OP_CONT = 1, 2, 8, 9, 10, 0


class WebSocketClosed(Exception):
    """The peer closed the socket; ``code`` is its close code when it sent one."""

    def __init__(self, code: int | None = None, reason: str = ""):
        super().__init__(f"websocket closed ({code}): {reason}")
        self.code = code
        self.reason = reason


class WebSocket:
    def __init__(self, url: str, *, headers: dict[str, str] | None = None, timeout: float = 30.0):
        parsed = urlparse(url)
        secure = parsed.scheme == "wss"
        port = parsed.port or (443 if secure else 80)
        raw = socket.create_connection((parsed.hostname, port), timeout=timeout)
        self._sock: socket.socket = (
            ssl.create_default_context().wrap_socket(raw, server_hostname=parsed.hostname)
            if secure else raw
        )
        self._buf = b""
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        key = base64.b64encode(os.urandom(16)).decode()
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {parsed.netloc}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self._sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in status + " ":
            raise WebSocketClosed(None, status)

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketClosed(None, "closed during the handshake")
            self._buf += chunk
        head, self._buf = self._buf.split(marker, 1)
        return head

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise WebSocketClosed(None, "connection lost")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(head + mask + masked)

    def send_json(self, frame: dict) -> None:
        self._send_frame(_OP_TEXT, json.dumps(frame).encode())

    def recv_json(self, timeout: float | None = None) -> dict | None:
        """The next JSON frame, or None at the timeout. Raises WebSocketClosed."""
        message = b""
        while True:
            if timeout is not None and not self._buf:
                ready, _, _ = select.select([self._sock], [], [], timeout)
                if not ready:
                    return None
            self._sock.settimeout(None if timeout is None else max(timeout, 30.0))
            first, second = self._read_exact(2)
            opcode, fin = first & 0x0F, bool(first & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            payload = self._read_exact(length)
            if opcode == _OP_PING:
                self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            if opcode == _OP_CLOSE:
                code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else None
                raise WebSocketClosed(code, payload[2:].decode(errors="replace"))
            message += payload
            if fin:
                return json.loads(message.decode())

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._send_frame(_OP_CLOSE, struct.pack(">H", 1000))
        with contextlib.suppress(OSError):
            self._sock.close()
