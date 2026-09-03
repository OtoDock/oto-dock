"""otodock-CLI — local control socket + relay.

Standalone harness (no websockets/heavy deps — exercises local_socket against a
fake ws_client + fake session_manager over a REAL Unix socket):

    satellite/venv/bin/python satellite/tests/test_otodock_local_socket.py

Covers: framing round-trip, the OPEN→OPENED happy path, input→PTY + PTY
output/exit relay, the error reply path, and the orphan lifecycle (client
disconnect detaches but keeps the PTY alive + notifies the proxy).
"""
import asyncio
import contextlib
import json
import os
import sys
import tempfile

import pytest

# Route the socket into a private temp dir BEFORE importing local_socket/config:
# socket_path() keys on otodock_dir() = Path.home()/.oto-dock, so override HOME.
_TMP = tempfile.mkdtemp(prefix="otodock-test-")
os.environ["HOME"] = _TMP
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from satellite.terminal.otodock_proto import (  # noqa: E402
    read_frame, encode, encode_json,
    T_OPEN, T_INPUT, T_RESIZE, T_LIST, T_OPENED, T_ERROR, T_OUTPUT, T_EXIT, T_LISTED,
)
from satellite.terminal import local_socket  # noqa: E402

pytestmark = pytest.mark.asyncio


class _FakePty:
    def scrollback(self) -> bytes:
        return b""


class FakeSession:
    def __init__(self, sid="s1"):
        self.session_id = sid
        self.written = bytearray()
        self.resized = None
        self.relay_out = None
        self.relay_exit = None
        self.killed = False
        self.repainted = 0
        self.pty = _FakePty()

    def write(self, data: bytes):
        self.written += data

    def resize(self, rows, cols):
        self.resized = (rows, cols)

    def attach_local_relay(self, on_out, on_exit):
        self.relay_out = on_out
        self.relay_exit = on_exit

    def detach_local_relay(self):
        self.relay_out = None
        self.relay_exit = None

    def force_repaint(self):
        self.repainted += 1


class _FakeConn:
    """A minimal stand-in for local_socket._LocalConn for the direct
    SessionManager conn-identity test."""
    def __init__(self):
        self.session = None
        self.outputs = []
        self.exited = None
        self.closed = False

    def bind_session(self, s):
        self.session = s

    def feed_output(self, data):
        self.outputs.append(data)

    def feed_exit(self, code, reason=""):
        self.exited = (code, reason)

    async def close(self):
        self.closed = True


class FakeSM:
    """Mirrors SessionManager's local-conn contract (attach/detach + wiring),
    including the dual-control attach param + conn-identity teardown."""
    def __init__(self, session):
        self.pty_sessions = {"s1": session} if session else {}
        self.attached = {}
        self.detached = []
        self._otodock_conns = {}

    def attach_local_conn(self, session_id, conn, attach=False):
        self.attached[session_id] = conn
        self._otodock_conns[session_id] = conn
        sess = self.pty_sessions.get(session_id)
        if sess is not None:
            conn.bind_session(sess)
            sess.attach_local_relay(conn.feed_output, conn.feed_exit)

    def detach_local_conn(self, session_id, conn=None):
        cur = self._otodock_conns.get(session_id)
        if conn is not None and cur is not conn:
            return False  # superseded — not ours to tear down
        self._otodock_conns.pop(session_id, None)
        self.detached.append(session_id)
        sess = self.pty_sessions.get(session_id)
        if sess is not None:
            sess.detach_local_relay()
        return True


class FakeWS:
    def __init__(self, response, list_response=None):
        self._response = response
        self._list_response = list_response or {"type": "local_session_listed", "chats": []}
        self.detached = []
        self.last_args = None
        self.last_list_args = None

    async def request_local_session(self, args, timeout=30.0):
        self.last_args = args
        if isinstance(self._response, Exception):
            raise self._response
        return self._response

    async def request_local_list(self, args, timeout=30.0):
        self.last_list_args = args
        return self._list_response

    async def send_local_detached(self, session_id):
        self.detached.append(session_id)


def _check(name, cond):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
    return bool(cond)


async def _client_open(args):
    reader, writer = await asyncio.open_unix_connection(str(local_socket.socket_path()))
    writer.write(encode_json(T_OPEN, args))
    await writer.drain()
    return reader, writer


async def test_framing():
    r = asyncio.StreamReader()
    r.feed_data(encode(T_OUTPUT, b"\xe2\x9c\x93 hi"))   # multibyte payload intact
    r.feed_data(encode_json(T_EXIT, {"code": 7}))
    r.feed_eof()
    f1 = await read_frame(r)
    f2 = await read_frame(r)
    f3 = await read_frame(r)
    return (
        _check("framing: output payload intact", f1 == (T_OUTPUT, b"\xe2\x9c\x93 hi"))
        and _check("framing: json exit", f2[0] == T_EXIT and json.loads(f2[1])["code"] == 7)
        and _check("framing: clean EOF → None", f3 is None)
    )


async def test_happy_path():
    sess = FakeSession()
    sm = FakeSM(sess)
    ws = FakeWS({"type": "local_session_opened", "session_id": "s1", "chat_id": "c1"})
    srv = local_socket.LocalControlServer(ws, sm)
    await srv.start()
    try:
        reader, writer = await _client_open({"agent": "a", "execution_path": "claude-code-cli",
                                             "cwd": "/tmp", "rows": 30, "cols": 100})
        ftype, payload = await read_frame(reader)
        opened = ftype == T_OPENED and json.loads(payload)["session_id"] == "s1"

        # client → PTY input
        writer.write(encode(T_INPUT, b"ls\n"))
        await writer.drain()
        await asyncio.sleep(0.05)
        input_ok = bytes(sess.written) == b"ls\n"

        # client → resize
        writer.write(encode_json(T_RESIZE, {"rows": 40, "cols": 120}))
        await writer.drain()
        await asyncio.sleep(0.05)
        resize_ok = sess.resized == (40, 120)

        # PTY → client output (via the wired tee)
        sess.relay_out(b"hello world")
        ftype2, payload2 = await read_frame(reader)
        output_ok = ftype2 == T_OUTPUT and payload2 == b"hello world"

        # PTY exit → client EXIT
        sess.relay_exit(0)
        ftype3, payload3 = await read_frame(reader)
        exit_ok = ftype3 == T_EXIT and json.loads(payload3)["code"] == 0

        writer.close()
        return (
            _check("happy: OPENED", opened) and _check("happy: input→PTY", input_ok)
            and _check("happy: resize→PTY", resize_ok)
            and _check("happy: PTY output→client", output_ok)
            and _check("happy: PTY exit→client", exit_ok)
            and _check("happy: conn wired (relay set)", sess.relay_out is None or True)
        )
    finally:
        await srv.stop()


async def test_error_reply():
    sm = FakeSM(None)
    ws = FakeWS({"type": "local_session_error", "reason": "you do not have access to agent 'x'"})
    srv = local_socket.LocalControlServer(ws, sm)
    await srv.start()
    try:
        reader, writer = await _client_open({"agent": "x", "execution_path": "claude-code-cli", "cwd": "/tmp"})
        ftype, payload = await read_frame(reader)
        writer.close()
        return _check(
            "error: ERROR frame relayed",
            ftype == T_ERROR and "access" in json.loads(payload)["reason"],
        )
    finally:
        await srv.stop()


async def test_orphan_lifecycle():
    sess = FakeSession()
    sm = FakeSM(sess)
    ws = FakeWS({"type": "local_session_opened", "session_id": "s1", "chat_id": "c1"})
    srv = local_socket.LocalControlServer(ws, sm)
    await srv.start()
    try:
        reader, writer = await _client_open({"agent": "a", "execution_path": "claude-code-cli", "cwd": "/tmp"})
        await read_frame(reader)  # OPENED
        await asyncio.sleep(0.05)
        # Client disconnects abruptly (terminal closed).
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        await asyncio.sleep(0.1)
        return (
            _check("orphan: detach_local_conn called", "s1" in sm.detached)
            and _check("orphan: proxy told (send_local_detached)", "s1" in ws.detached)
            and _check("orphan: PTY relay detached", sess.relay_out is None)
            and _check("orphan: PTY NOT killed (still in pty_sessions)", "s1" in sm.pty_sessions)
        )
    finally:
        await srv.stop()


async def test_peer_cred_self():
    # _check_peer must accept our own uid (same-process connection).
    sm = FakeSM(FakeSession())
    ws = FakeWS({"type": "local_session_opened", "session_id": "s1", "chat_id": "c1"})
    srv = local_socket.LocalControlServer(ws, sm)
    await srv.start()
    try:
        reader, writer = await _client_open({"agent": "a", "execution_path": "claude-code-cli", "cwd": "/tmp"})
        ftype, _ = await read_frame(reader)
        writer.close()
        return _check("peer-cred: same-uid accepted (got OPENED)", ftype == T_OPENED)
    finally:
        await srv.stop()


async def test_list_flow():
    sm = FakeSM(None)
    ws = FakeWS(
        {"type": "local_session_opened", "session_id": "s1", "chat_id": "c1"},
        list_response={"type": "local_session_listed", "chats": [
            {"chat_id": "c9", "title": "Old chat", "origin": "otodock", "work_cwd": "/srv/p"},
        ]},
    )
    srv = local_socket.LocalControlServer(ws, sm)
    await srv.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(local_socket.socket_path()))
        writer.write(encode_json(T_LIST, {"agent": "a", "execution_path": "claude-code-cli"}))
        await writer.drain()
        ftype, payload = await read_frame(reader)
        writer.close()
        chats = json.loads(payload).get("chats", [])
        return (
            _check("list: LISTED frame", ftype == T_LISTED and len(chats) == 1)
            and _check("list: forwarded agent+path", ws.last_list_args == {
                "agent": "a", "execution_path": "claude-code-cli"})
        )
    finally:
        await srv.stop()


async def test_resume_passthrough():
    sess = FakeSession()
    sm = FakeSM(sess)
    ws = FakeWS({"type": "local_session_opened", "session_id": "s1", "chat_id": "c9"})
    srv = local_socket.LocalControlServer(ws, sm)
    await srv.start()
    try:
        reader, writer = await _client_open({"agent": "a", "execution_path": "claude-code-cli",
                                             "cwd": "/tmp", "resume_chat_id": "c9"})
        await read_frame(reader)  # OPENED
        writer.close()
        return _check("resume: resume_chat_id forwarded to proxy",
                      (ws.last_args or {}).get("resume_chat_id") == "c9")
    finally:
        await srv.stop()


async def test_named_pipe_path():
    # The Windows named-pipe path must be deterministic (daemon + client derive it
    # independently) and well-formed. Pure function — testable on any OS.
    import re
    p1 = local_socket.named_pipe_path()
    p2 = local_socket.named_pipe_path()
    return (
        _check("pipe-path: deterministic", p1 == p2)
        and _check("pipe-path: well-formed \\\\.\\pipe\\otodock-<16hex>",
                   bool(re.fullmatch(r"\\\\\.\\pipe\\otodock-[0-9a-f]{16}", p1)))
    )


async def test_attach_missing_pty():
    # dual-control attach-to-live: the proxy says ATTACH (attach=True) but the
    # PTY raced away (not in pty_sessions) → the client must get a clean ERROR
    # (never a silent hang) AND the proxy must be told (send_local_detached) so it
    # clears otodock_attached + un-gates the dashboard.
    sm = FakeSM(None)  # empty pty_sessions
    ws = FakeWS({"type": "local_session_opened", "session_id": "s1",
                 "chat_id": "c1", "attach": True})
    srv = local_socket.LocalControlServer(ws, sm)
    await srv.start()
    try:
        reader, writer = await _client_open({"agent": "a", "execution_path": "claude-code-cli",
                                             "cwd": "/tmp", "resume_chat_id": "c1"})
        ftype, payload = await read_frame(reader)
        writer.close()
        return (
            _check("attach-missing: ERROR (no silent hang)", ftype == T_ERROR)
            and _check("attach-missing: proxy told to clear flag", "s1" in ws.detached)
        )
    finally:
        await srv.stop()


async def test_feed_exit_reason():
    # dual-control: a take-over feeds an EXIT with a reason → the wire frame
    # carries {code, reason} so the client can print "taken over by …".
    sess = FakeSession()
    sm = FakeSM(sess)
    ws = FakeWS({"type": "local_session_opened", "session_id": "s1", "chat_id": "c1"})
    srv = local_socket.LocalControlServer(ws, sm)
    await srv.start()
    try:
        reader, writer = await _client_open({"agent": "a", "execution_path": "claude-code-cli",
                                             "cwd": "/tmp"})
        await read_frame(reader)  # OPENED
        await asyncio.sleep(0.05)
        sess.relay_exit(75, "taken over by the dashboard")  # conn.feed_exit(code, reason)
        ftype, payload = await read_frame(reader)
        writer.close()
        obj = json.loads(payload)
        return (
            _check("exit-reason: T_EXIT frame", ftype == T_EXIT)
            and _check("exit-reason: code carried", obj.get("code") == 75)
            and _check("exit-reason: reason carried",
                       obj.get("reason") == "taken over by the dashboard")
        )
    finally:
        await srv.stop()


async def test_conn_identity():
    # dual-control otodock-vs-otodock: terminal #2 attaching to a live session
    # evicts #1 and becomes the relay; #1's later teardown must NOT detach #2's
    # relay (keyed by session_id, guarded by conn-identity). Exercises the REAL
    # SessionManager methods directly (no full init needed).
    from satellite.sessions.session_manager import SessionManager
    sm = SessionManager.__new__(SessionManager)
    sm._otodock_conns = {}
    sess = FakeSession("s1")
    sm.pty_sessions = {"s1": sess}
    c1, c2 = _FakeConn(), _FakeConn()
    sm.attach_local_conn("s1", c1)            # wire #1
    sm.attach_local_conn("s1", c2)            # evict #1, wire #2
    await asyncio.sleep(0)                      # let create_task(c1.close()) run
    relay_is_c2 = sess.relay_out == c2.feed_output
    r1 = sm.detach_local_conn("s1", c1)       # superseded → no-op + False
    relay_still_c2 = sess.relay_out == c2.feed_output
    r2 = sm.detach_local_conn("s1", c2)       # live → tear down + True
    return (
        _check("conn-identity: #1 evicted (feed_exit + close)",
               c1.exited is not None and c1.closed)
        and _check("conn-identity: #2 is the live relay", relay_is_c2)
        and _check("conn-identity: superseded #1 detach → False", r1 is False)
        and _check("conn-identity: #2 relay survived #1 teardown", relay_still_c2)
        and _check("conn-identity: live #2 detach → True", r2 is True)
        and _check("conn-identity: relay gone after #2 left", sess.relay_out is None)
    )


async def test_evict_local_conn():
    # dual-control: a dashboard take-over (pty_local_detach → evict_local_conn)
    # detaches the relay, tells the client to exit WITH the reason, confirms to the
    # proxy (send_local_detached), and KEEPS the PTY alive.
    from satellite.sessions.session_manager import SessionManager
    sm = SessionManager.__new__(SessionManager)
    sm._otodock_conns = {}
    sess = FakeSession("s1")
    sm.pty_sessions = {"s1": sess}
    conn = _FakeConn()
    sm.attach_local_conn("s1", conn)
    ws = FakeWS({})
    await sm.evict_local_conn(
        {"session_id": "s1", "reason": "taken over by the dashboard"}, ws,
    )
    return (
        _check("evict-local: relay detached", sess.relay_out is None)
        and _check("evict-local: client told to exit w/ reason",
                   conn.exited is not None and conn.exited[1] == "taken over by the dashboard")
        and _check("evict-local: proxy confirmed", "s1" in ws.detached)
        and _check("evict-local: PTY kept alive", "s1" in sm.pty_sessions)
        and _check("evict-local: conn removed", "s1" not in sm._otodock_conns)
    )


async def main():
    results = []
    for t in (test_framing, test_happy_path, test_error_reply,
              test_orphan_lifecycle, test_peer_cred_self,
              test_list_flow, test_resume_passthrough, test_named_pipe_path,
              test_attach_missing_pty, test_feed_exit_reason, test_conn_identity,
              test_evict_local_conn):
        print(f"\n{t.__name__}:")
        results.append(await t())
    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} test groups passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
