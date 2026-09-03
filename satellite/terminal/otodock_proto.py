"""otodock-CLI — the local control-socket wire protocol.

Shared by the satellite's local control server (:mod:`local_socket`) and the
``otodock`` client. A frame is::

    [ 4-byte big-endian length N ][ 1 type byte ][ N-1 payload bytes ]

Control payloads (OPEN / RESIZE / OPENED / ERROR / EXIT) are UTF-8 JSON. The I/O
lanes (INPUT / OUTPUT) carry **raw PTY bytes verbatim** — no decode/encode at the
relay, because a terminal is a byte stream and CSI / multibyte sequences must not
be split or re-encoded across frames.
"""
from __future__ import annotations

import asyncio
import json
import struct

# client → satellite
T_OPEN = 0x01     # JSON {agent, execution_path, cwd, model, mode, rows, cols, term?, resume_chat_id?}
T_INPUT = 0x02    # raw bytes → PTY stdin
T_RESIZE = 0x03   # JSON {rows, cols}
T_DETACH = 0x04   # (no payload) graceful client detach
T_LIST = 0x05     # JSON {agent, execution_path} — request resumable chat history

# satellite → client
T_OPENED = 0x81   # JSON {session_id, chat_id}
T_ERROR = 0x82    # JSON {reason}
T_OUTPUT = 0x83   # raw bytes ← PTY stdout
T_EXIT = 0x84     # JSON {code, reason}
T_LISTED = 0x85   # JSON {chats: [{chat_id, title, updated_at, origin, work_cwd}]}

_HEADER = struct.Struct(">I")
MAX_FRAME = 8 * 1024 * 1024  # 8 MiB hard guard against a desync/garbage stream

# Dual-control: the T_EXIT code used when a session is TAKEN OVER (the
# dashboard claimed it, or another otodock terminal attached) rather than the CLI
# exiting on its own. The client distinguishes a take-over by the presence of a
# `reason`, not this code; the code is just what the shell sees (distinct from a
# clean 0 and from a real CLI exit code). 75 = BSD EX_TEMPFAIL ("try again later").
SUPERSEDE_EXIT_CODE = 75


def encode(ftype: int, payload: bytes = b"") -> bytes:
    body = bytes([ftype]) + payload
    return _HEADER.pack(len(body)) + body


def encode_json(ftype: int, obj) -> bytes:
    return encode(ftype, json.dumps(obj).encode("utf-8"))


async def read_frame(reader: "asyncio.StreamReader") -> "tuple[int, bytes] | None":
    """Read one frame. Returns ``(type, payload)``, or ``None`` on clean EOF.
    Raises ``ValueError`` on a malformed/oversized frame."""
    try:
        header = await reader.readexactly(4)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    (n,) = _HEADER.unpack(header)
    if n < 1 or n > MAX_FRAME:
        raise ValueError(f"bad otodock frame length {n}")
    try:
        body = await reader.readexactly(n)
    except (asyncio.IncompleteReadError, ConnectionError):
        return None
    return body[0], body[1:]
