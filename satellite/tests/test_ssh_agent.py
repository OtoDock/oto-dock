"""Tests for satellite/sessions/ssh_agent.py: the session's SSH agent
socket, each message relayed to the proxy's hook.

The HTTP opener is a stand-in that records the request and answers one
message; the proxy-side route and its auth are the proxy suite's.
"""

import base64
import contextlib
import ctypes
import json
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import urllib.error
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from satellite._vendored import sandbox_render as render
from satellite.config import SatelliteConfig
from satellite.sessions import sandbox_host, session_files, ssh_agent, ssh_agent_pipe
from satellite.tests.pipe_standin import StandinPipes

SID = "3f9a1b2c-4d5e-6f70-8192-a3b4c5d6e7f8"
NAME = "3f9a1b2c4d5e.sock"
KEYS = {
    "prod_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample prod",
    "other": "ssh-rsa AAAAB3NzaC1yc2E other",
}
IDENTITIES_ANSWER = b"\x0c\x00\x00\x00\x00"
REQUEST_IDENTITIES = b"\x0b"


@pytest.fixture
def roots(monkeypatch):
    """A short OtoDock root (a socket path has 100 bytes) with the secrets
    root under it; every door is stopped after the test."""
    root = Path(tempfile.mkdtemp(prefix="oto-ssh-"))
    monkeypatch.setattr(ssh_agent, "otodock_dir", lambda: root)
    monkeypatch.setattr(session_files, "_secrets_root", lambda: root / "session-secrets")
    yield root
    for sid in list(ssh_agent._doors):
        ssh_agent.stop(sid)
    shutil.rmtree(root, ignore_errors=True)


class _Response:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


class _Proxy:
    """The opener's stand-in: records each request; answers one message,
    or raises what it was given."""

    def __init__(self, answer):
        self.answer = answer
        self.requests = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        if isinstance(self.answer, Exception):
            raise self.answer
        body = json.dumps({"message_b64": base64.b64encode(self.answer).decode()})
        return _Response(body.encode())


@pytest.fixture
def proxy(monkeypatch):
    fake = _Proxy(IDENTITIES_ANSWER)
    monkeypatch.setattr(ssh_agent, "_opener", fake)
    return fake


def _payload(**kw):
    base = {
        "env": {"PROXY_URL": "http://127.0.0.1:9", "PROXY_API_KEY": "jwt"},
        "ssh_agent": {"public_keys": dict(KEYS)},
    }
    base.update(kw)
    return base


def _client(path: str) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    sock.connect(path)
    return sock


def _recv(sock: socket.socket, n: int) -> bytes:
    data = b""
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _ask(sock: socket.socket, message: bytes) -> bytes:
    """One framed message in; the framed answer out (``b""`` at EOF)."""
    sock.sendall(struct.pack(">I", len(message)) + message)
    head = _recv(sock, 4)
    if len(head) < 4:
        return head
    return head + _recv(sock, struct.unpack(">I", head)[0])


# ---------------------------------------------------------------------------
# the socket
# ---------------------------------------------------------------------------

def test_the_socket_relays_a_message_and_its_answer(roots, proxy):
    env = ssh_agent.start(SID, _payload())
    sock = _client(env["SSH_AUTH_SOCK"])
    try:
        assert _ask(sock, REQUEST_IDENTITIES) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
        assert _ask(sock, b"\x0d" + b"\x01" * 40)[:4] == b"\x00\x00\x00\x05"
    finally:
        sock.close()
    assert len(proxy.requests) == 2
    req = proxy.requests[0]
    assert req.full_url == "http://127.0.0.1:9/v1/hooks/ssh-agent" and req.get_method() == "POST"
    assert req.get_header("Authorization") == "Bearer jwt"
    assert json.loads(req.data) == {
        "session_id": SID, "message_b64": base64.b64encode(REQUEST_IDENTITIES).decode(),
    }
    assert json.loads(proxy.requests[1].data)["message_b64"] == base64.b64encode(b"\x0d" + b"\x01" * 40).decode()


def test_a_compute_payload_posts_to_this_machines_link_port(roots, proxy):
    env = ssh_agent.start(SID, _payload(
        tunnel_port=43210, env={"PROXY_URL": "http://127.0.0.1:8400", "PROXY_API_KEY": "jwt"},
    ))
    sock = _client(env["SSH_AUTH_SOCK"])
    try:
        assert _ask(sock, REQUEST_IDENTITIES) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
    finally:
        sock.close()
    assert proxy.requests[0].full_url == "http://127.0.0.1:43210/v1/hooks/ssh-agent"


@pytest.mark.parametrize("failure", [
    urllib.error.HTTPError("http://127.0.0.1:9/v1/hooks/ssh-agent", 403, "Forbidden", None, None),
    urllib.error.URLError("connection refused"),
    OSError("timed out"),
    b"",
    b"\x00" * (ssh_agent.MAX_MESSAGE_BYTES + 1),
], ids=["http-403", "unreachable", "os-error", "empty-answer", "oversized-answer"])
def test_a_failed_post_answers_the_failure_message(roots, proxy, failure):
    proxy.answer = failure
    env = ssh_agent.start(SID, _payload())
    sock = _client(env["SSH_AUTH_SOCK"])
    try:
        assert _ask(sock, REQUEST_IDENTITIES) == ssh_agent.FAILURE_FRAME == b"\x00\x00\x00\x01\x05"
        # The connection stays usable: the next message is relayed.
        proxy.answer = IDENTITIES_ANSWER
        assert _ask(sock, REQUEST_IDENTITIES) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
    finally:
        sock.close()


def test_a_zero_or_oversized_length_ends_the_connection(roots, proxy):
    env = ssh_agent.start(SID, _payload())
    for length in (0, ssh_agent.MAX_MESSAGE_BYTES + 1):
        sock = _client(env["SSH_AUTH_SOCK"])
        try:
            sock.sendall(struct.pack(">I", length))
            assert sock.recv(1) == b""
        finally:
            sock.close()
    assert proxy.requests == []
    # A message at the cap is relayed.
    sock = _client(env["SSH_AUTH_SOCK"])
    try:
        big = b"\x0d" + b"\x00" * (ssh_agent.MAX_MESSAGE_BYTES - 1)
        assert _ask(sock, big) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
    finally:
        sock.close()
    assert base64.b64decode(json.loads(proxy.requests[0].data)["message_b64"]) == big


# ---------------------------------------------------------------------------
# the files and the env
# ---------------------------------------------------------------------------

def test_the_keys_land_and_the_env_names_both_variables(roots, proxy, caplog):
    payload = _payload()
    payload["ssh_agent"]["public_keys"].update({
        "../evil": "ssh-rsa X", ".": "ssh-rsa X", "dir/key": "ssh-rsa X", "": "ssh-rsa X",
        "multi": "ssh-rsa A\nssh-rsa B", "blank": "   ", "notext": 7,
    })
    with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
        env = ssh_agent.start(SID, payload)
    keys = roots / "session-secrets" / SID / "ssh"
    sock_path = roots / "run" / "ssh-agent" / NAME
    assert env == {"OTO_SSH_KEY_DIR": str(keys), "SSH_AUTH_SOCK": str(sock_path)}
    assert sorted(p.name for p in keys.iterdir()) == ["other.pub", "prod_key.pub"]
    assert (keys / "prod_key.pub").read_text() == KEYS["prod_key"] + "\n"
    assert (keys / "prod_key.pub").stat().st_mode & 0o777 == 0o600
    assert keys.stat().st_mode & 0o777 == 0o700
    assert keys.parent.stat().st_mode & 0o777 == 0o700
    assert not (roots / "session-secrets" / "evil").exists()
    assert caplog.text.count("refusing public key") == 7
    assert sock_path.is_socket() and sock_path.stat().st_mode & 0o777 == 0o600
    assert sock_path.parent.stat().st_mode & 0o777 == 0o700
    assert sock_path.parent.parent.stat().st_mode & 0o777 == 0o700
    assert render._SSH_AGENT_NAME_RE.match(sock_path.name)
    # Idempotent per session id.
    assert ssh_agent.start(SID, payload) == env
    assert list(ssh_agent._doors) == [SID]


def test_a_payload_without_ssh_agent_opens_nothing(roots, proxy):
    assert ssh_agent.start(SID, {"env": {"PROXY_URL": "http://127.0.0.1:9", "PROXY_API_KEY": "jwt"}}) == {}
    assert ssh_agent.start(SID, _payload(ssh_agent="yes")) == {}
    assert not (roots / "run").exists() and not (roots / "session-secrets").exists()
    assert ssh_agent._doors == {}


def test_no_public_keys_means_no_key_dir(roots, proxy):
    env = ssh_agent.start(SID, _payload(ssh_agent={}))
    assert set(env) == {"SSH_AUTH_SOCK"} and Path(env["SSH_AUTH_SOCK"]).is_socket()
    assert not (roots / "session-secrets").exists()
    ssh_agent.stop(SID)
    assert ssh_agent.start(SID, _payload(ssh_agent={"public_keys": {}})) == env


def test_stop_unlinks_the_socket_and_removes_the_files(roots, proxy):
    env = ssh_agent.start(SID, _payload())
    sock = _client(env["SSH_AUTH_SOCK"])
    try:
        # A round trip first: the client is accepted and held by the door.
        assert _ask(sock, REQUEST_IDENTITIES) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
        ssh_agent.stop(SID)
        assert not Path(env["SSH_AUTH_SOCK"]).exists()
        assert not Path(env["OTO_SSH_KEY_DIR"]).exists()
        # The held client is dropped: EOF, or a reset when the peer closed first.
        with contextlib.suppress(ConnectionResetError):
            assert sock.recv(1) == b""
    finally:
        sock.close()
    assert SID not in ssh_agent._doors
    ssh_agent.stop(SID)
    ssh_agent.stop("never-seen")
    # The session can open again at the same path.
    assert ssh_agent.start(SID, _payload()) == env


def test_the_socket_name_is_the_proxys_rule():
    assert ssh_agent.socket_name(SID) == NAME
    assert ssh_agent.socket_name("ABCDEF-123456789012345") == "123456789012.sock"
    assert ssh_agent.socket_name("s-1") == "1.sock"
    assert ssh_agent.socket_name("xyz") == ""


def test_a_session_id_without_hex_gets_no_socket(roots, proxy, caplog):
    with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
        env = ssh_agent.start("xyz", _payload())
    assert set(env) == {"OTO_SSH_KEY_DIR"} and "no hex characters" in caplog.text
    assert not (roots / "run").exists()


def test_a_second_session_with_the_same_name_gets_no_socket(roots, proxy, caplog):
    first = ssh_agent.start(SID, _payload())
    with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
        second = ssh_agent.start(SID + "-twin", _payload())
    assert set(second) == {"OTO_SSH_KEY_DIR"} and "another session holds" in caplog.text
    assert Path(first["SSH_AUTH_SOCK"]).is_socket()
    ssh_agent.stop(SID + "-twin")
    assert Path(first["SSH_AUTH_SOCK"]).is_socket()


def test_a_long_socket_path_is_refused_and_the_keys_still_land(roots, proxy, monkeypatch, caplog):
    monkeypatch.setattr(ssh_agent, "otodock_dir", lambda: roots / ("x" * 90))
    with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
        env = ssh_agent.start(SID, _payload())
    assert set(env) == {"OTO_SSH_KEY_DIR"}
    assert (Path(env["OTO_SSH_KEY_DIR"]) / "prod_key.pub").is_file()
    assert f"exceeds {ssh_agent.MAX_SOCKET_PATH_BYTES} bytes" in caplog.text


def test_a_payload_without_the_token_gets_no_socket(roots, proxy, caplog):
    with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
        env = ssh_agent.start(SID, _payload(env={"PROXY_URL": "http://127.0.0.1:9"}))
    assert set(env) == {"OTO_SSH_KEY_DIR"} and "no proxy URL or session token" in caplog.text


def test_a_new_start_while_the_old_door_still_shuts_keeps_its_socket(roots, proxy):
    """Two shuts of one door: the second returns at once while the first
    still closes. A new start of the same session in that window binds the
    same path, and the old shut's end removes only its own file."""
    env = ssh_agent.start(SID, _payload())
    door = ssh_agent._doors.pop(SID)
    entered, release = threading.Event(), threading.Event()
    real_shutdown = door.server.shutdown

    def slow_shutdown():
        entered.set()
        release.wait(5)
        real_shutdown()

    door.server.shutdown = slow_shutdown
    first = threading.Thread(target=ssh_agent._shut, args=(door,))
    first.start()
    assert entered.wait(5)
    # The name goes first, while the old server still holds its file.
    assert not Path(env["SSH_AUTH_SOCK"]).exists()
    ssh_agent._shut(door)
    again = ssh_agent.start(SID, _payload())
    assert again["SSH_AUTH_SOCK"] == env["SSH_AUTH_SOCK"]
    release.set()
    first.join(timeout=5)
    assert not first.is_alive()
    sock = _client(again["SSH_AUTH_SOCK"])
    try:
        assert _ask(sock, REQUEST_IDENTITIES) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
    finally:
        sock.close()


def test_a_door_shut_after_a_new_one_bound_its_path_leaves_that_file(roots, proxy):
    """A new start of the same session that binds before the old door's
    shut runs: the old shut removes no file but its own."""
    env = ssh_agent.start(SID, _payload())
    old = ssh_agent._doors.pop(SID)
    again = ssh_agent.start(SID, _payload())
    assert again["SSH_AUTH_SOCK"] == env["SSH_AUTH_SOCK"]
    ssh_agent._shut(old)
    assert Path(again["SSH_AUTH_SOCK"]).is_socket()
    sock = _client(again["SSH_AUTH_SOCK"])
    try:
        assert _ask(sock, REQUEST_IDENTITIES) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
    finally:
        sock.close()


def test_a_bind_waits_for_a_shut_that_is_removing_its_file(roots, proxy, monkeypatch):
    """A new start that comes while the old door checks and removes its
    file binds only after that removal: its own file survives."""
    env = ssh_agent.start(SID, _payload())
    old = ssh_agent._doors.pop(SID)
    real_lstat = os.lstat
    race = {}

    def lstat_then_race(path, *args, **kwargs):
        st = real_lstat(path, *args, **kwargs)
        if "thread" not in race and str(path) == env["SSH_AUTH_SOCK"]:
            race["thread"] = threading.Thread(
                target=lambda: race.setdefault("env", ssh_agent.start(SID, _payload())))
            race["thread"].start()
            race["thread"].join(0.3)
        return st

    monkeypatch.setattr(ssh_agent.os, "lstat", lstat_then_race)
    ssh_agent._shut(old)
    race["thread"].join(5)
    monkeypatch.setattr(ssh_agent.os, "lstat", real_lstat)
    assert Path(race["env"]["SSH_AUTH_SOCK"]).is_socket()


def test_a_start_that_was_stopped_binds_nothing_over_a_new_door(roots, proxy, monkeypatch):
    """The first start has passed its check that no other door holds the
    path, and then waits. A stop takes its door. A new start of the same
    session id binds. The first start goes on: the new door keeps its file."""
    run = roots / "run"
    entered, release = threading.Event(), threading.Event()
    real_chmod = os.chmod
    first_thread = {}

    def chmod(path, mode, *args, **kwargs):
        if (str(path) == str(run) and threading.current_thread() is first_thread.get("t")
                and not entered.is_set()):
            entered.set()
            release.wait(10)
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(ssh_agent.os, "chmod", chmod)
    out = {}
    first = threading.Thread(target=lambda: out.setdefault("first", ssh_agent.start(SID, _payload())))
    first_thread["t"] = first
    first.start()
    assert entered.wait(5)
    stopper = threading.Thread(target=ssh_agent.stop, args=(SID,))
    stopper.start()
    for _ in range(500):
        if SID not in ssh_agent._doors:
            break
        time.sleep(0.01)
    assert SID not in ssh_agent._doors
    second = ssh_agent.start(SID, _payload())
    path = Path(second["SSH_AUTH_SOCK"])
    assert path.is_socket()
    release.set()
    first.join(10)
    stopper.join(10)
    assert not first.is_alive() and not stopper.is_alive()
    assert out["first"] == {}
    assert ssh_agent._doors[SID].path == path
    assert path.is_socket()


def test_a_start_stopped_while_it_opens_binds_nothing(roots, proxy, monkeypatch, caplog):
    """A start stopped between its folders and its bind, with no newer door
    yet: it binds no socket at all, and the log says why."""
    run = roots / "run"
    entered, release = threading.Event(), threading.Event()
    real_chmod = os.chmod
    first_thread = {}
    binds = []

    def chmod(path, mode, *args, **kwargs):
        if (str(path) == str(run) and threading.current_thread() is first_thread.get("t")
                and not entered.is_set()):
            entered.set()
            release.wait(10)
        return real_chmod(path, mode, *args, **kwargs)

    class CountingServer(ssh_agent._AgentServer):
        def __init__(self, *args, **kwargs):
            binds.append(threading.current_thread())
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(ssh_agent.os, "chmod", chmod)
    monkeypatch.setattr(ssh_agent, "_AgentServer", CountingServer)
    out = {}
    first = threading.Thread(target=lambda: out.setdefault("first", ssh_agent.start(SID, _payload())))
    first_thread["t"] = first
    first.start()
    assert entered.wait(5)
    stopper = threading.Thread(target=ssh_agent.stop, args=(SID,))
    stopper.start()
    for _ in range(500):
        if SID not in ssh_agent._doors:
            break
        time.sleep(0.01)
    with caplog.at_level("INFO", logger="satellite.ssh_agent"):
        release.set()
        first.join(10)
        stopper.join(10)
    assert out["first"] == {} and binds == []
    assert "the session was stopped while it opened" in caplog.text


def test_a_session_of_the_same_name_that_binds_first_keeps_its_socket(roots, proxy, monkeypatch, caplog):
    """A start waits before its bind while another session of the same
    socket name binds. The waiting start reads that the path is held under
    the bind's lock and binds nothing: the other session keeps its file."""
    run = roots / "run"
    entered, release = threading.Event(), threading.Event()
    real_chmod = os.chmod
    first_thread = {}

    def chmod(path, mode, *args, **kwargs):
        if (str(path) == str(run) and threading.current_thread() is first_thread.get("t")
                and not entered.is_set()):
            entered.set()
            release.wait(10)
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(ssh_agent.os, "chmod", chmod)
    out = {}
    first = threading.Thread(target=lambda: out.setdefault("first", ssh_agent.start(SID, _payload())))
    first_thread["t"] = first
    first.start()
    assert entered.wait(5)
    twin = ssh_agent.start(SID + "-twin", _payload())
    path = Path(twin["SSH_AUTH_SOCK"])
    assert path.is_socket()
    with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
        release.set()
        first.join(10)
    assert not first.is_alive()
    assert "SSH_AUTH_SOCK" not in out["first"] and "another session holds" in caplog.text
    twin_door = ssh_agent._doors[SID + "-twin"]
    assert twin_door.path == path and path.is_socket()
    assert (os.lstat(path).st_dev, os.lstat(path).st_ino) == twin_door.identity


def test_a_stale_socket_file_is_replaced(roots, proxy):
    stale = roots / "run" / "ssh-agent" / NAME
    stale.parent.mkdir(parents=True)
    stale.write_bytes(b"")
    env = ssh_agent.start(SID, _payload())
    assert Path(env["SSH_AUTH_SOCK"]) == stale and stale.is_socket()


# ---------------------------------------------------------------------------
# Windows: a named pipe, served by the same relay (its thin layer is the
# stand-in here; test_ssh_agent_pipe.py has the tests that call Windows)
# ---------------------------------------------------------------------------

WINDOWS_SSH = "C:/Windows/System32/OpenSSH/ssh.exe"


@pytest.fixture
def windows(monkeypatch, roots):
    """This machine as a Windows one, Windows' ssh installed, no git
    setting of the person's own."""
    cfg = ssh_agent.satellite_config
    monkeypatch.setattr(cfg, "HOST", cfg.ROWS[cfg.WINDOWS])
    pipes = StandinPipes()
    monkeypatch.setattr(ssh_agent_pipe, "api", lambda: pipes)
    monkeypatch.setattr(ssh_agent, "_windows_ssh", lambda: WINDOWS_SSH)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    return pipes


def test_windows_serves_a_pipe_of_its_own_and_names_the_systems_ssh(windows, proxy, roots):
    keys = roots / "session-secrets" / SID / "ssh"
    env = ssh_agent.start(SID, _payload())
    name = env.pop("SSH_AUTH_SOCK")
    assert env == {"OTO_SSH_KEY_DIR": str(keys), "OTO_SSH": WINDOWS_SSH, "GIT_SSH": WINDOWS_SSH}
    assert name.startswith(ssh_agent_pipe.PIPE_PREFIX) and SID[:8] not in name
    assert (keys / "prod_key.pub").is_file()
    assert not (roots / "run").exists()
    windows.wait_listening(name)
    client = windows.open_client(name)
    try:
        assert _ask(client, REQUEST_IDENTITIES) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
    finally:
        client.close()
    req = proxy.requests[0]
    assert req.full_url == "http://127.0.0.1:9/v1/hooks/ssh-agent"
    assert req.get_header("Authorization") == "Bearer jwt"
    assert json.loads(req.data)["session_id"] == SID
    # Idempotent per session id.
    again = ssh_agent.start(SID, _payload())
    assert again["SSH_AUTH_SOCK"] == name


def test_windows_relays_through_the_one_relay(windows, proxy):
    """The failure frame, a zero or oversized length, a message at the cap:
    the unix socket's rules, over the pipe."""
    name = ssh_agent.start(SID, _payload())["SSH_AUTH_SOCK"]
    windows.write_limit = 3
    proxy.answer = urllib.error.URLError("connection refused")
    windows.wait_listening(name)
    client = windows.open_client(name)
    try:
        assert _ask(client, REQUEST_IDENTITIES) == ssh_agent.FAILURE_FRAME
        proxy.answer = IDENTITIES_ANSWER
        assert _ask(client, REQUEST_IDENTITIES) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
    finally:
        client.close()
    for length in (0, ssh_agent.MAX_MESSAGE_BYTES + 1):
        windows.wait_listening(name)
        client = windows.open_client(name)
        try:
            client.sendall(struct.pack(">I", length))
            assert client.recv(1) == b""
        finally:
            client.close()
    assert len(proxy.requests) == 2


def test_windows_gives_each_start_a_fresh_name(windows, proxy):
    first = ssh_agent.start(SID, _payload())["SSH_AUTH_SOCK"]
    other = ssh_agent.start("aa11bb22-0000-4000-8000-000000000000", _payload())["SSH_AUTH_SOCK"]
    assert first != other
    ssh_agent.stop(SID)
    again = ssh_agent.start(SID, _payload())["SSH_AUTH_SOCK"]
    assert again not in (first, other)
    with pytest.raises(FileNotFoundError):
        windows.open_client(first)


def test_windows_stop_cuts_the_client_and_frees_the_name(windows, proxy, roots):
    env = ssh_agent.start(SID, _payload())
    name = env["SSH_AUTH_SOCK"]
    windows.wait_listening(name)
    client = windows.open_client(name)
    try:
        assert _ask(client, REQUEST_IDENTITIES) == b"\x00\x00\x00\x05" + IDENTITIES_ANSWER
        door = ssh_agent._doors[SID]
        ssh_agent.stop(SID)
        assert client.recv(1) == b""
    finally:
        client.close()
    assert windows.open_instances(name) == 0
    with pytest.raises(FileNotFoundError):
        windows.open_client(name)
    assert door.server is None and not door.thread.is_alive()
    assert all(stop.closed for stop in windows.stops) and not windows.double_closes
    assert not Path(env["OTO_SSH_KEY_DIR"]).exists()
    ssh_agent.stop(SID)


def test_windows_stop_during_the_start_leaves_no_pipe(windows, proxy, monkeypatch):
    stoppers = []
    real_env = ssh_agent._windows_ssh_env

    def stopped_meanwhile():
        stopper = threading.Thread(target=ssh_agent.stop, args=(SID,))
        stopper.start()
        # The stop takes the door at once and waits for the start to finish.
        for _ in range(500):
            if SID not in ssh_agent._doors:
                break
            time.sleep(0.01)
        stoppers.append(stopper)
        return real_env()

    monkeypatch.setattr(ssh_agent, "_windows_ssh_env", stopped_meanwhile)
    assert ssh_agent.start(SID, _payload()) == {}
    stoppers[0].join(timeout=10)
    assert windows.names == {} and not windows.double_closes
    assert all(stop.closed and not stop.double_closes for stop in windows.stops)
    assert SID not in ssh_agent._doors


def test_windows_a_held_name_gives_no_endpoint_and_the_keys_still_land(windows, proxy, roots, monkeypatch, caplog):
    held = ssh_agent_pipe.PIPE_PREFIX + "f" * 32
    windows.create(held, ssh_agent_pipe.open_mode(True), ssh_agent_pipe.pipe_mode(), "O:S-1-5-21-9D:P(A;;GA;;;WD)")
    monkeypatch.setattr(ssh_agent_pipe, "pipe_name", lambda: held)
    with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
        env = ssh_agent.start(SID, _payload())
    assert env == {"OTO_SSH_KEY_DIR": str(roots / "session-secrets" / SID / "ssh")}
    assert "no pipe at" in caplog.text
    assert windows.open_instances(held) == 1


@pytest.mark.parametrize("failure", [
    OSError(None, "CreateNamedPipeW: The pipe is being closed", None, 232),
    ctypes.ArgumentError("argument 8: wrong type"),
    AttributeError("function 'CreateNamedPipeW' not found"),
    "the layer itself",
], ids=["os-error", "argument-error", "attribute-error", "layer-not-built"])
def test_windows_a_layer_that_fails_costs_the_endpoint_alone(windows, proxy, roots, monkeypatch, caplog, failure):
    """Whatever the thin Windows layer raises, the session starts: the keys
    land and only the endpoint is missing, with the reason in the log and no
    secret in it."""
    if failure == "the layer itself":
        def broken():
            raise AttributeError("module 'ctypes' has no attribute 'WinDLL'")
        monkeypatch.setattr(ssh_agent_pipe, "api", broken)
    else:
        def refuse(*args):
            raise failure
        windows.create = refuse
    with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
        env = ssh_agent.start(SID, _payload())
    assert env == {"OTO_SSH_KEY_DIR": str(roots / "session-secrets" / SID / "ssh")}
    assert (roots / "session-secrets" / SID / "ssh" / "prod_key.pub").is_file()
    assert "no pipe at" in caplog.text and "jwt" not in caplog.text
    assert ssh_agent._doors[SID].server is None
    ssh_agent.stop(SID)


def test_windows_without_the_token_opens_no_pipe(windows, proxy, caplog):
    with caplog.at_level("WARNING", logger="satellite.ssh_agent"):
        env = ssh_agent.start(SID, _payload(env={"PROXY_URL": "http://127.0.0.1:9"}))
    assert "SSH_AUTH_SOCK" not in env and "OTO_SSH" not in env
    assert windows.creates == [] and "no pipe" in caplog.text


@pytest.mark.parametrize("own", ["GIT_SSH", "GIT_SSH_COMMAND"])
def test_windows_leaves_a_persons_own_git_ssh_alone(windows, proxy, monkeypatch, own):
    monkeypatch.setenv(own, "plink")
    env = ssh_agent.start(SID, _payload())
    assert env["OTO_SSH"] == WINDOWS_SSH and "GIT_SSH" not in env


def test_windows_without_its_openssh_client_names_no_ssh(windows, proxy, monkeypatch):
    monkeypatch.setattr(ssh_agent, "_windows_ssh", lambda: "")
    env = ssh_agent.start(SID, _payload())
    assert "SSH_AUTH_SOCK" in env and "OTO_SSH" not in env and "GIT_SSH" not in env


def test_linux_and_macos_get_no_windows_variables(roots, proxy, monkeypatch):
    monkeypatch.setattr(ssh_agent, "_windows_ssh", lambda: WINDOWS_SSH)
    env = ssh_agent.start(SID, _payload())
    assert set(env) == {"SSH_AUTH_SOCK", "OTO_SSH_KEY_DIR"}


@pytest.mark.parametrize("wow64, asked", [
    ("", "D:\\Win\\System32\\OpenSSH\\ssh.exe"),
    ("AMD64", "D:\\Win\\Sysnative\\OpenSSH\\ssh.exe"),
])
def test_windows_ssh_is_the_fixed_system_path_never_a_path_lookup(monkeypatch, wow64, asked):
    seen = []
    monkeypatch.setenv("SystemRoot", "D:\\Win")
    if wow64:
        monkeypatch.setenv("PROCESSOR_ARCHITEW6432", wow64)
    else:
        monkeypatch.delenv("PROCESSOR_ARCHITEW6432", raising=False)
    monkeypatch.setattr(ssh_agent.os.path, "isfile", lambda path: seen.append(path) or True)
    monkeypatch.setattr(ssh_agent.shutil, "which", lambda *a, **k: pytest.fail("a PATH lookup"))
    assert ssh_agent._windows_ssh() == "D:/Win/System32/OpenSSH/ssh.exe"
    assert seen == [asked]
    monkeypatch.setattr(ssh_agent.os.path, "isfile", lambda path: False)
    assert ssh_agent._windows_ssh() == ""


def test_merge_env_lets_the_payload_win_but_never_the_ambient_value():
    config = {"env": {"SSH_AUTH_SOCK": "/tmp/oto-ssh-agent.sock", "OTO_SSH_KEY_DIR": "/cfg/ssh"}}
    additions = {"SSH_AUTH_SOCK": "/run/a.sock", "OTO_SSH_KEY_DIR": "/secrets/ssh"}
    env = dict(config["env"])
    ssh_agent.merge_env(env, additions, config)
    assert env == config["env"]
    env = {"SSH_AUTH_SOCK": "/the/users/own/agent.sock", "PATH": "/bin"}
    ssh_agent.merge_env(env, additions, {"env": {"PROXY_URL": "http://127.0.0.1:9"}})
    assert env == {"SSH_AUTH_SOCK": "/run/a.sock", "OTO_SSH_KEY_DIR": "/secrets/ssh", "PATH": "/bin"}
    ssh_agent.merge_env(env, {}, {})
    assert env["SSH_AUTH_SOCK"] == "/run/a.sock"


# ---------------------------------------------------------------------------
# the compute render
# ---------------------------------------------------------------------------

def test_the_compute_render_host_names_the_socket_dir(roots, proxy, monkeypatch, tmp_path):
    monkeypatch.setattr(sandbox_host, "otodock_dir", lambda: roots)
    monkeypatch.setattr(sandbox_host, "_bwrap_has_size", lambda: True)
    monkeypatch.setattr(render, "mem_total_mb", lambda: 16000)
    monkeypatch.setattr(render, "host_resolv_has_loopback_ns", lambda *_a: False)
    cfg = SatelliteConfig(machine_id="c-1", machine_secret="s", platform_url="ws://127.0.0.1:8400/v1/satellite",
                          agents_dir=tmp_path / "agents", mcps_dir=tmp_path / "mcps", compute=True)
    host = sandbox_host.render_host(cfg, tunnel_port=43210)
    assert host.ssh_agent_dir == str(ssh_agent.socket_dir()) == str(roots / "run" / "ssh-agent")
    desc = render.SandboxDescription(
        agent="a", binds=(), mcp_binds=(), mcps_root="", extra_binds=(), cwd="/users/alice",
        proxy_port=8400, forwards=(), ssh_agent=NAME,
    )
    with pytest.raises(render.RenderRefused):
        render.ssh_agent_source(desc, host)
    env = ssh_agent.start(SID, _payload(tunnel_port=43210, ssh_agent={}))
    assert render.ssh_agent_source(desc, host) == env["SSH_AUTH_SOCK"]


# ---------------------------------------------------------------------------
# the sessions' wiring
# ---------------------------------------------------------------------------

@pytest.fixture
def sat_config(tmp_path):
    return SatelliteConfig(machine_id="m-1", machine_secret="s", platform_url="ws://127.0.0.1:8400/v1/satellite",
                           agents_dir=tmp_path / "agents", mcps_dir=tmp_path / "mcps")


@pytest.fixture
def wired(monkeypatch):
    """Stand-ins for the module's entry points and session_files' wipe: the
    calls in order, with the env a start answers."""
    calls: list[tuple[str, str]] = []

    def _start(sid, _config):
        calls.append(("start", sid))
        return {"SSH_AUTH_SOCK": "/run/a.sock", "OTO_SSH_KEY_DIR": "/run/ssh"}

    monkeypatch.setattr(ssh_agent, "start", _start)
    monkeypatch.setattr(ssh_agent, "stop", lambda sid: calls.append(("stop", sid)))
    monkeypatch.setattr(session_files, "wipe", lambda sid: calls.append(("wipe", sid)))
    return calls


@pytest.mark.asyncio
async def test_the_cli_session_starts_the_agent_and_stops_it_at_close(
        tmp_path, sat_config, wired, monkeypatch):
    from satellite.sessions.cli_session import CLISession
    monkeypatch.setenv("SSH_AUTH_SOCK", "/the/users/own/agent.sock")
    agent_dir = tmp_path / "agents" / "a"
    agent_dir.mkdir(parents=True)
    config = {
        "cwd_relative": "users/alice", "claude_dir_relative": "users/alice/.claude",
        "system_prompt": "p", "mcp_config": {"mcpServers": {}}, "model": "m", "effort": "high",
        "env": {"PROXY_URL": "http://127.0.0.1:9", "PROXY_API_KEY": "jwt", "OTO_CLI_TOKEN": "t",
                "ANTHROPIC_API_KEY": "k"},
    }
    proc = AsyncMock()
    proc.pid = 4242
    proc.returncode = None
    proc.stdout = AsyncMock()
    proc.stderr = AsyncMock()
    proc.stderr.read = AsyncMock(return_value=b"")
    proc.stdout.readline = AsyncMock(
        return_value=json.dumps({"type": "system", "subtype": "init", "mcp_servers": []}).encode() + b"\n")
    seen: dict = {}

    async def _exec(*_cmd, **kw):
        seen.update(kw)
        return proc
    session = CLISession("sess-1", agent_dir, config, sat_config)
    with patch("asyncio.create_subprocess_exec", side_effect=_exec):
        await session.start()
    proc.returncode = 0
    await session.close()
    assert seen["env"]["SSH_AUTH_SOCK"] == "/run/a.sock" and seen["env"]["OTO_SSH_KEY_DIR"] == "/run/ssh"
    assert wired == [("start", "sess-1"), ("stop", "sess-1"), ("wipe", "sess-1")]


@pytest.mark.asyncio
async def test_the_codex_session_starts_the_agent_and_stops_it_at_close(
        tmp_path, sat_config, wired):
    from satellite.sessions.codex_session import CodexSession
    agent_dir = tmp_path / "agents" / "a"
    agent_dir.mkdir(parents=True)
    config = {
        "cwd_relative": "users/alice", "codex_dir_relative": "users/alice/.codex",
        "system_prompt": "p", "agents_md_content": "# a", "mcp_config_toml": "",
        "model": "gpt-5.4", "effort": "high",
        "env": {"PROXY_URL": "http://127.0.0.1:9", "PROXY_API_KEY": "jwt", "CODEX_API_KEY": "k",
                "SSH_AUTH_SOCK": "/the/payloads.sock"},
    }
    session = CodexSession("sess-2", agent_dir, config, sat_config)
    client = AsyncMock()
    client.proc = None

    async def _request(_method, _params=None):
        return {"thread": {"id": "t-1"}}
    client.request = _request
    seen: dict = {}

    async def _connect(env):
        seen.update(env)
        session._client = client
    with patch.object(session, "_connect_with_retry", new=AsyncMock(side_effect=_connect)), \
            patch.object(session, "_warm_mcps", new=AsyncMock()), \
            patch.object(session, "_run_forwarder", new=AsyncMock()):
        await session.start()
        await session.close()
    # The payload named the socket: its value stays; the key dir lands.
    assert seen["SSH_AUTH_SOCK"] == "/the/payloads.sock" and seen["OTO_SSH_KEY_DIR"] == "/run/ssh"
    assert wired == [("start", "sess-2"), ("stop", "sess-2"), ("wipe", "sess-2")]


@pytest.mark.asyncio
@pytest.mark.parametrize("compute", [False, True])
async def test_a_terminal_session_starts_the_agent_with_its_files_and_stops_it_at_close(
        tmp_path, wired, compute):
    """The PTY sessions take the socket through the files step the headless
    ones run too, and release it at close; a compute terminal, whose files
    are the platform's, still releases the socket."""
    from satellite.terminal.pty_session_base import BasePtySession
    session = BasePtySession.__new__(BasePtySession)
    session.session_id = "sess-3"
    session.agent_dir = tmp_path
    session.config = {"env": {"PROXY_URL": "http://127.0.0.1:9", "PROXY_API_KEY": "jwt"}}
    session._tail_stop = False
    session._tail_task = None
    session.pty = None
    session.superseded = False
    session._compute = compute
    session._own_copy = False

    async def _nothing():
        return None
    session._forward_transcript_delta = _nothing
    session._scan_and_forward_files = _nothing
    session._on_close = lambda: None
    env = await session._session_agent_env()
    assert env["SSH_AUTH_SOCK"] == "/run/a.sock" and env["OTO_SSH_KEY_DIR"] == "/run/ssh"
    await session.close()
    expected = [("start", "sess-3"), ("stop", "sess-3")]
    assert wired == expected + ([] if compute else [("wipe", "sess-3")])


def _wipe_threads(monkeypatch) -> list:
    """The thread each prepared-files removal runs on."""
    import threading
    seen: list = []
    monkeypatch.setattr(session_files, "wipe_agent_files", lambda sid: seen.append(threading.get_ident()))
    return seen


@pytest.mark.asyncio
async def test_a_cli_sessions_close_removes_its_prepared_files_off_the_loop(tmp_path, sat_config, wired, monkeypatch):
    """A compute session's close removes its prepared files (up to 400 walks
    beneath the agent's folder) in a worker thread, never on the loop."""
    import threading

    from satellite.sessions.cli_session import CLISession
    agent_dir = tmp_path / "agents" / "a"
    agent_dir.mkdir(parents=True)
    session = CLISession("sess-1", agent_dir, {"env": {}}, sat_config)
    session._compute, session._own_copy = True, True
    seen = _wipe_threads(monkeypatch)
    await session.close()
    assert seen and seen[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_a_codex_sessions_close_removes_its_prepared_files_off_the_loop(tmp_path, sat_config, wired, monkeypatch):
    import threading

    from satellite.sessions.codex_session import CodexSession
    agent_dir = tmp_path / "agents" / "a"
    agent_dir.mkdir(parents=True)
    session = CodexSession("sess-2", agent_dir, {"env": {}, "codex_dir_relative": "users/alice/.codex"}, sat_config)
    session._compute, session._own_copy = True, True
    seen = _wipe_threads(monkeypatch)
    await session.close()
    assert seen and seen[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_a_terminals_close_removes_its_prepared_files_off_the_loop(tmp_path, wired, monkeypatch):
    import threading

    from satellite.terminal.pty_session_base import BasePtySession
    session = BasePtySession.__new__(BasePtySession)
    session.session_id = "sess-3"
    session.agent_dir = tmp_path
    session.config = {"env": {}}
    session._tail_stop = False
    session._tail_task = None
    session.pty = None
    session.superseded = False
    session._compute, session._own_copy = True, True

    async def _nothing():
        return None
    session._forward_transcript_delta = _nothing
    session._scan_and_forward_files = _nothing
    session._on_close = lambda: None
    seen = _wipe_threads(monkeypatch)
    await session.close()
    assert seen and seen[0] != threading.get_ident()


def test_the_post_goes_direct_whatever_proxy_the_environment_names(monkeypatch):
    """PROXY_URL is this machine's own loopback tunnel: an environment proxy
    (or Windows' Internet Options one, which never exempts 127.0.0.1) must
    not capture the agent's post."""
    import socket
    import threading
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            data = b'{"message_b64": ""}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
    for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, dead)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    req = urllib.request.Request(f"http://127.0.0.1:{server.server_address[1]}/v1/hooks/ssh-agent",
                                 data=b"{}", method="POST")
    try:
        with ssh_agent._opener.open(req, timeout=10) as resp:
            got = resp.read()
    finally:
        server.shutdown()
        server.server_close()
    assert got == b'{"message_b64": ""}'

