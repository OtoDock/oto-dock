"""The ``otodock`` verbs that use a personal API key, against a small fake of
the public routes: a login's file, send and wait through a running turn, a
retry that keeps its request id, the exit codes, and the refusal inside a
session."""

from __future__ import annotations

import json
import os
import stat
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from satellite.terminal import otodock_api, otodock_cli

KEY = "otok_" + "k" * 43


class _Fake(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def _answer(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _route(self) -> None:
        srv = self.server
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        srv.seen.append((self.command, self.path, body))
        if self.headers.get("Authorization") != f"Bearer {KEY}":
            return self._answer(401, {"detail": "This key is not valid."})
        path = self.path.split("?")[0]
        if path == "/v1/api/me":
            return self._answer(200, {"name": "Ada", "email": "a@x", "key": {
                "name": "walk", "scopes": ["chat"], "agents": [], "expires_at": None,
                "run_without_asking": False}})
        if path == "/v1/api/agents":
            return self._answer(200, {"agents": [{"agent": "pa", "role": "editor", "name": "PA",
                                                  "description": "", "shared_chats": False}]})
        if path == "/v1/api/chats" and self.command == "POST":
            return self._answer(200, {"id": "c1", "agent": body["agent"], "title": "",
                                      "permission_mode": body["mode"], "updated_at": "", "shared": False})
        if path == "/v1/api/chats/c1/messages":
            turn = srv.turns.pop(0)
            return self._answer(turn.pop("_status", 200), turn)
        if path.startswith("/v1/api/chats/c1/messages/"):
            return self._answer(200, srv.turns.pop(0))
        if path == "/v1/api/tasks" and self.command == "POST":
            return self._answer(200, {"task_id": "t1", "run_id": "r1"})
        if path == "/v1/api/runs/r1":
            return self._answer(200, srv.runs.pop(0))
        if path == "/v1/api/notifications":
            return self._answer(200, {"id": "n1"})
        return self._answer(403, {"detail": "An API key cannot use this route."})

    do_GET = do_POST = do_PUT = _route


def _turn(state: str, **extra) -> dict:
    return {"chat_id": "c1", "message_id": 7, "request_id": "r", "state": state, "answer": "",
            "waiting_for_you": False, "detail": "", **extra}


@pytest.fixture
def api(tmp_path, monkeypatch):
    for name in ("OTO_SESSION_ID", "PROXY_API_KEY", "OTODOCK_URL", "OTODOCK_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(otodock_api.Path, "home", lambda: tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Fake)
    server.seen, server.turns, server.runs = [], [], []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("OTODOCK_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("OTODOCK_API_KEY", KEY)
    yield server
    server.shutdown()


def test_login_saves_the_key_for_this_user_only(api, tmp_path, capsys):
    assert otodock_api.main(["login"]) == 0
    saved = tmp_path / ".config" / "otodock" / "cli.json"
    assert json.loads(saved.read_text())["key"] == KEY
    if os.name != "nt":
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert "Ada" in capsys.readouterr().out


def test_on_a_paired_machine_the_key_sits_in_the_machines_own_folder(api, tmp_path):
    (tmp_path / ".oto-dock").mkdir()
    (tmp_path / ".oto-dock" / "satellite.conf").write_text("[x]")
    assert otodock_api.main(["login"]) == 0
    assert (tmp_path / ".oto-dock" / "cli.json").is_file()


def test_a_refused_key_says_why_and_saves_nothing(api, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OTODOCK_API_KEY", "otok_wrong")
    assert otodock_api.main(["login"]) == 1
    assert "not valid" in capsys.readouterr().err
    assert not (tmp_path / ".config" / "otodock" / "cli.json").exists()


def test_a_redirect_is_refused_and_the_key_goes_nowhere_else(api, monkeypatch, capsys):
    """A login gateway that does not let ``/v1/api/`` through answers with
    a redirect to its sign-in page: the key never travels with it."""
    target = f"http://127.0.0.1:{api.server_address[1]}/v1/api/agents"

    class _Gateway(BaseHTTPRequestHandler):
        def log_message(self, *_a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.end_headers()

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("OTODOCK_URL", f"http://127.0.0.1:{gateway.server_address[1]}")
        assert otodock_api.main(["agents"]) == 1
    finally:
        gateway.shutdown()
    assert api.seen == []
    err = capsys.readouterr().err
    assert "redirect" in err and "/v1/api/" in err


def test_a_saved_key_is_never_sent_to_another_address(api, monkeypatch, capsys):
    assert otodock_api.main(["login"]) == 0
    api.seen.clear()
    # The address alone, from the environment: the saved key would follow it.
    monkeypatch.delenv("OTODOCK_API_KEY")
    assert otodock_api.main(["agents"]) == 1
    assert api.seen == [] and "OTODOCK_API_KEY" in capsys.readouterr().err
    monkeypatch.delenv("OTODOCK_URL")
    assert otodock_api.main(["agents"]) == 0


def test_login_warns_about_a_plain_address_before_the_key_leaves(api, monkeypatch):
    events: list[tuple[str, str]] = []
    real = otodock_api._call

    def _call(url, key, method, path, **kw):
        events.append(("call", path))
        return real(url, key, method, path, **kw)

    monkeypatch.setattr(otodock_api, "_say", lambda text: events.append(("say", text)))
    monkeypatch.setattr(otodock_api, "_call", _call)
    assert otodock_api.main(["login"]) == 0
    assert events[0][0] == "say" and "not encrypted" in events[0][1]
    assert events[1] == ("call", "/v1/api/me")


@pytest.mark.parametrize("name", ["OTO_SESSION_ID", "PROXY_API_KEY"])
def test_every_verb_refuses_inside_a_session(api, monkeypatch, capsys, name):
    monkeypatch.setenv(name, "x")
    for argv in (["agents"], ["chat", "send", "--agent", "pa", "hi"], ["login"]):
        assert otodock_api.main(argv) == 1
    assert "session" in capsys.readouterr().err
    assert api.seen == []


def test_send_and_wait_prints_the_answer_through_a_running_turn(api, capsys):
    api.turns = [_turn("running"), _turn("running", waiting_for_you=True, detail="Open the chat"),
                 _turn("done", answer="Hello")]
    assert otodock_api.main(["chat", "send", "--agent", "pa", "--wait", "hi"]) == 0
    out = capsys.readouterr()
    assert out.out.strip() == "Hello" and "Open the chat" in out.err
    posts = [b for m, p, b in api.seen if m == "POST" and p.endswith("/messages")]
    assert len(posts) == 1 and posts[0]["text"] == "hi" and posts[0]["request_id"]


def test_a_dropped_connection_retries_with_the_same_request_id(api, monkeypatch):
    real = otodock_api._call
    calls = {"n": 0}

    def flaky(url, key, method, path, **kw):
        if path.endswith("/messages"):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("dropped")
        return real(url, key, method, path, **kw)

    monkeypatch.setattr(otodock_api, "_call", flaky)
    monkeypatch.setattr(otodock_api.time, "sleep", lambda _s: None)
    api.turns = [_turn("done", answer="ok")]
    assert otodock_api.main(["chat", "send", "--chat", "c1", "--wait", "hi"]) == 0
    assert calls["n"] == 2


def test_the_exit_codes_of_a_turn(api, capsys):
    api.turns = [_turn("running")]
    assert otodock_api.main(["chat", "send", "--chat", "c1", "--wait", "--timeout", "0", "hi"]) == 3
    # Waiting on a person: said once, not again at the timeout.
    capsys.readouterr()
    waits = _turn("running", waiting_for_you=True, detail="Open the chat")
    api.turns = [dict(waits) for _ in range(5000)]
    assert otodock_api.main(["chat", "send", "--chat", "c1", "--wait", "--timeout", "1", "hi"]) == 3
    assert capsys.readouterr().err.count("Open the chat") == 1
    api.turns = [_turn("not_sent", detail="The message was not sent.")]
    assert otodock_api.main(["chat", "send", "--chat", "c1", "--wait", "hi"]) == 1
    assert "not sent" in capsys.readouterr().err
    assert otodock_api.main(["chat", "send", "--chat", "c1", "--agent", "pa", "hi"]) == 2


def test_tasks_run_and_notify(api, capsys):
    api.runs = [{"status": "running"}, {"status": "completed", "output_text": "Done."}]
    otodock_api.time.sleep, real = (lambda _s: None), otodock_api.time.sleep
    try:
        assert otodock_api.main(["tasks", "run", "--agent", "pa", "--wait", "go", "now"]) == 0
    finally:
        otodock_api.time.sleep = real
    assert "Done." in capsys.readouterr().out
    body = next(b for m, p, b in api.seen if p == "/v1/api/tasks")
    assert body["prompt"] == "go now" and body["scope"] == "user"
    assert otodock_api.main(["notify", "Built", "ok"]) == 0


def test_the_otodock_command_hands_the_verbs_over(api, capsys):
    assert otodock_cli.main(["agents"]) == 0
    assert "pa\teditor\tPA" in capsys.readouterr().out
