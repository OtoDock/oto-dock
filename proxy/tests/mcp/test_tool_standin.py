"""The stand-in (``core/stdio_path_interceptor.py`` with ``OTO_TOOL_STANDIN``):
it answers the engine's handshake and lists from the saved listing with no
program running, starts the program once at the first call it cannot answer,
says in one sentence why a start failed, and saves what a program answered.
Driven as the engine drives it: the real script in a subprocess, a fake tool
program, and a fake platform for its two routes and the secrets fetch."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from core import stdio_path_interceptor as itc
from tests._paths import PROXY_DIR

SCRIPT = PROXY_DIR / "core" / "stdio_path_interceptor.py"

# A tool program: writes a line to its start log, answers the handshake, its
# list (``FAKE_TOOLS``) and a call with the call's arguments; asks the engine
# for its roots before its list when ``FAKE_ASK_ROOTS`` is set; exits at once
# when ``FAKE_DIE`` is set; waits ``FAKE_SLOW`` seconds before it answers and
# ``FAKE_LIST_SLOW`` before the engine's own list; says its list changed
# after its first list (``FAKE_CHANGED=start``, the stand-in's own) or the
# engine's (``engine``); logs a log level it is given, and "end" at the end
# of its input.
FAKE_PROGRAM = r'''
import json, os, sys, time
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("start\n")
if os.environ.get("FAKE_DIE"):
    sys.exit(3)
time.sleep(float(os.environ.get("FAKE_SLOW") or 0))
tools = json.loads(os.environ.get("FAKE_TOOLS") or "[]")
def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()
for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "protocolVersion": msg["params"]["protocolVersion"],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "fake", "version": "1.0"}}})
    elif method == "notifications/initialized":
        with open(os.environ["FAKE_LOG"], "a") as fh:
            fh.write("initialized\n")
    elif method == "notifications/cancelled":
        with open(os.environ["FAKE_LOG"], "a") as fh:
            fh.write("cancelled\n")
    elif method == "logging/setLevel":
        with open(os.environ["FAKE_LOG"], "a") as fh:
            fh.write("level " + msg["params"]["level"] + "\n")
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
    elif method == "tools/list":
        if not str(msg["id"]).startswith("otodock-"):
            time.sleep(float(os.environ.get("FAKE_LIST_SLOW") or 0))
        if os.environ.get("FAKE_ASK_ROOTS"):
            send({"jsonrpc": "2.0", "id": "r1", "method": "roots/list"})
            answer = json.loads(sys.stdin.readline())
            with open(os.environ["FAKE_LOG"], "a") as fh:
                fh.write("roots " + json.dumps(answer.get("result")) + "\n")
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": tools}})
        whose = "start" if str(msg["id"]).startswith("otodock-") else "engine"
        if os.environ.get("FAKE_CHANGED") == whose:
            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    elif method == "tools/call":
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {
            "content": [{"type": "text", "text": "ran " + msg["params"]["name"]}]}})
    elif "id" in msg:
        send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write("end\n")
'''

TOOLS = [{"name": "echo", "description": "Echo.", "inputSchema": {"type": "object", "properties": {}}}]
LISTING = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                          "serverInfo": {"name": "fake", "version": "1.0"}, "instructions": "Use echo."},
           "lists": {"tools/list": TOOLS}}


class _Platform:
    """The platform's side: the listing, the admission and the secrets."""

    def __init__(self, *, listing=None, ask=None, credentials=200, kept=None, report_delay=0.0, start_status=None):
        self.listing = listing
        self.ask = ask or {"ok": True}
        # ``credentials`` and ``start_status`` may be a list: one answer per
        # request, the last one repeating (a restart: 503 first, then 200).
        self.start_status = list(start_status or [200])
        # What ``started`` answers about the keep (None: an older platform's
        # answer, with no word on it), after ``report_delay`` seconds.
        self.kept = kept
        self.report_delay = report_delay
        self.credentials = credentials
        self.calls: list[tuple[str, str, dict | None]] = []
        self.raw: list[bytes] = []
        platform = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                platform.calls.append(("GET", self.path, None))
                if self.path.startswith("/v1/hooks/tool-listing") and platform.listing is not None:
                    self._answer(200, {"listing": platform.listing})
                else:
                    self._answer(404, {"detail": "none"})

            def do_POST(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size)
                platform.raw.append(raw)
                body = json.loads(raw or b"null")
                platform.calls.append(("POST", self.path, body))
                if self.path == "/v1/hooks/tool-start":
                    phase = body.get("phase")
                    if phase == "started":
                        time.sleep(platform.report_delay)
                        answer = {"ok": True} if platform.kept is None else {"ok": True, "kept": platform.kept}
                    elif phase == "start" and platform._next("start_status") != 200:
                        self._answer(503, {"error": "tunnel-not-connected"})
                        return
                    else:
                        answer = platform.ask if phase == "start" else {"ok": True}
                    self._answer(200, answer)
                elif self.path == "/v1/hooks/mcp-credentials":
                    status = platform._next("credentials")
                    if status == 200:
                        self._answer(200, {"env": {"FAKE_SECRET_SEEN": "1"}})
                    else:
                        self._answer(status, {"detail": "no"})
                else:
                    self._answer(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def _next(self, name: str) -> int:
        seq = getattr(self, name)
        if not isinstance(seq, list):
            return seq
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def posts(self, phase: str) -> list[dict]:
        return [b for m, p, b in self.calls if p == "/v1/hooks/tool-start" and b and b.get("phase") == phase]

    def close(self):
        self.server.shutdown()


class _Engine:
    """The engine's side: writes lines to the stand-in, reads its answers."""

    def __init__(self, tmp_path: Path, platform: _Platform, *, marker=None, extra_env=None):
        self.log = tmp_path / "program.log"
        program = tmp_path / "fake_program.py"
        program.write_text(FAKE_PROGRAM)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OTO_", "PROXY_"))}
        env.update({
            "PROXY_URL": platform.url, "OTO_MCP_FETCH_TOKEN": "cap-token", "OTO_SESSION_ID": "s" * 36,
            "FAKE_LOG": str(self.log), "FAKE_TOOLS": json.dumps(TOOLS),
            itc.STANDIN_ENV: json.dumps(marker or {"v": 1, "stamp": "1.0", "account": "",
                                                    "fetch": False, "label": "Fake"}),
            **(extra_env or {}),
        })
        self.proc = subprocess.Popen(
            [sys.executable, "-I", str(SCRIPT), "--", sys.executable, "-I", str(program)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        self.lines: list[dict] = []
        self._ready = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            with self._ready:
                self.lines.append(json.loads(line))
                self._ready.notify_all()

    def send(self, obj: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def wait_for(self, pred, timeout: float = 15.0) -> dict:
        deadline = time.monotonic() + timeout
        with self._ready:
            while True:
                for msg in self.lines:
                    if pred(msg):
                        return msg
                left = deadline - time.monotonic()
                assert left > 0, f"no such line; got {self.lines}"
                self._ready.wait(left)

    def answer(self, rid) -> dict:
        return self.wait_for(lambda m: m.get("id") == rid and "method" not in m)

    def handshake(self) -> dict:
        self.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {"roots": {}},
            "clientInfo": {"name": "engine", "version": "1"}}})
        init = self.answer(1)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return init

    def starts(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def close(self) -> int:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        return self.proc.wait(timeout=15)


@pytest.fixture
def rig(tmp_path):
    made: list = []

    def make(**platform_kw):
        engine_kw = {k: platform_kw.pop(k) for k in ("marker", "extra_env") if k in platform_kw}
        platform = _Platform(**platform_kw)
        engine = _Engine(tmp_path, platform, **engine_kw)
        made.append((platform, engine))
        return platform, engine

    yield make
    for platform, engine in made:
        if engine.proc.poll() is None:
            engine.proc.kill()
        platform.close()


def test_a_saved_listing_answers_the_handshake_and_the_list_with_no_program(rig):
    platform, engine = rig(listing=LISTING)
    init = engine.handshake()["result"]
    # The engine asked for a newer version than the program's: the older one.
    assert init["protocolVersion"] == "2025-06-18"
    assert init["instructions"] == "Use echo."
    assert init["capabilities"]["tools"]["listChanged"] is True
    engine.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert [t["name"] for t in engine.answer(2)["result"]["tools"]] == ["echo"]
    # The engine has the list: said once (a terminal's first message waits
    # for it), never again for a second list.
    engine.send({"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}})
    engine.answer(5)
    deadline = time.monotonic() + 5
    while not platform.posts("listed") and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.2)
    assert platform.posts("listed") == [{"phase": "listed"}]
    engine.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert engine.answer(3)["result"] == {}
    engine.send({"jsonrpc": "2.0", "id": 4, "method": "prompts/list"})
    assert engine.answer(4)["error"]["code"] == -32601  # the program declares no prompts
    assert engine.starts() == []
    assert engine.close() == 0
    assert engine.starts() == []


@pytest.mark.parametrize("listing", [LISTING, None], ids=["saved", "nothing saved"])
def test_a_probe_before_the_handshake_is_answered_and_starts_nothing(rig, listing):
    """Claude Code 2.1.322 sends ``server/discover`` before ``initialize`` and
    falls back on an error, as the SDK servers answer it."""
    platform, engine = rig(listing=listing)
    engine.send({"jsonrpc": "2.0", "id": "server-discover-probe-1", "method": "server/discover"})
    assert engine.answer("server-discover-probe-1")["error"]["code"] == -32601
    assert engine.starts() == []
    engine.handshake()
    assert engine.starts() == ([] if listing else ["start", "initialized"])
    engine.close()


def test_the_first_call_starts_the_program_once_and_it_answers(rig):
    platform, engine = rig(listing=LISTING)
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    engine.send({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(7)["result"]["content"][0]["text"] == "ran echo"
    assert engine.answer(8)["result"]["content"][0]["text"] == "ran echo"
    # One start, one handshake of the program's own: the engine's was answered already.
    assert engine.starts() == ["start", "initialized"]
    ask, started = platform.posts("start"), platform.posts("started")
    assert ask and ask[0]["with_session"] is False
    deadline = time.monotonic() + 5
    while not platform.posts("started") and time.monotonic() < deadline:
        time.sleep(0.05)
    started = platform.posts("started")
    assert started[0]["listing"] is None  # the list it saved was right
    assert started[0]["ms"] >= 0
    engine.close()


def test_a_call_to_a_name_outside_the_list_never_starts_the_program(rig):
    platform, engine = rig(listing=LISTING)
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}})
    assert engine.answer(5)["error"]["code"] == -32602
    assert engine.starts() == []
    assert platform.posts("start") == []
    engine.close()


def test_a_refused_admission_answers_the_sentence_and_starts_nothing(rig):
    sentence = "There is not enough free memory to start this tool right now. Try again in a few minutes."
    platform, engine = rig(listing=LISTING, ask={"ok": False, "message": sentence})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    result = engine.answer(9)["result"]
    assert result["isError"] is True and result["content"][0]["text"] == sentence
    assert engine.starts() == []
    # The box has room again: the next call starts the program.
    platform.ask = {"ok": True}
    engine.send({"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(10)["result"]["content"][0]["text"] == "ran echo"
    assert engine.starts()[0] == "start"
    engine.close()


def test_a_program_that_stops_before_it_answers_says_so_and_the_stand_in_stays(rig):
    platform, engine = rig(listing=LISTING, extra_env={"FAKE_DIE": "1"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    text = engine.answer(11)["result"]["content"][0]["text"]
    assert text == "The Fake tool did not start. Its program stopped before it answered."
    engine.send({"jsonrpc": "2.0", "id": 12, "method": "tools/list"})
    assert engine.answer(12)["result"]["tools"][0]["name"] == "echo"
    assert engine.close() == 0


def test_every_call_held_behind_a_failed_start_hears_the_sentence(rig):
    """A chat never hangs on a call queued behind a start that failed."""
    platform, engine = rig(listing=LISTING, extra_env={"FAKE_DIE": "1"})
    engine.handshake()
    for rid in (21, 22):
        engine.send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                     "params": {"name": "echo", "arguments": {}}})
    for rid in (21, 22):
        assert "did not start" in engine.answer(rid)["result"]["content"][0]["text"]
    engine.close()


def test_a_start_that_fails_after_its_box_counted_it_gives_the_program_back(rig):
    platform, engine = rig(listing=LISTING, extra_env={"FAKE_DIE": "1"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 31, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    engine.answer(31)
    deadline = time.monotonic() + 5
    while not platform.posts("failed") and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(platform.posts("failed")) == 1
    engine.close()


def test_an_engine_that_leaves_during_a_counted_start_gives_the_program_back(rig):
    """The engine goes away (an MCP reconnect) while the program it asked
    for starts: the stand-in ends, and before it does the platform hears
    that the start it counted did not happen."""
    platform, engine = rig(listing=LISTING, extra_env={"FAKE_SLOW": "5"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 33, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    deadline = time.monotonic() + 10
    while engine.starts() != ["start"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert engine.starts() == ["start"]
    engine.close()
    assert len(platform.posts("failed")) == 1


def test_a_start_the_box_refused_gives_nothing_back(rig):
    platform, engine = rig(listing=LISTING, ask={"ok": False, "message": "No room."})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 32, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    engine.answer(32)
    time.sleep(0.5)
    assert platform.posts("failed") == []
    engine.close()


def test_a_server_with_no_tools_is_not_told_to_have_them(rig):
    prompts_only = {"initialize": {"protocolVersion": "2025-06-18", "capabilities": {"prompts": {}},
                                   "serverInfo": {"name": "fake", "version": "1.0"}},
                    "lists": {"prompts/list": []}}
    platform, engine = rig(listing=prompts_only)
    caps = engine.handshake()["result"]["capabilities"]
    assert "tools" not in caps and "prompts" in caps
    engine.close()


def test_a_tool_whose_secrets_cannot_be_fetched_never_starts(rig):
    platform, engine = rig(listing=LISTING, credentials=404,
                           marker={"v": 1, "stamp": "1.0", "account": "a", "fetch": True, "label": "Fake"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 13, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    text = engine.answer(13)["result"]["content"][0]["text"]
    assert text == "The Fake tool cannot start in this chat any more. Reload the chat or start a new one."
    assert engine.starts() == []
    engine.close()


def test_a_call_held_while_the_platform_was_away_is_answered_and_never_run_late(rig):
    """A restart under a first call: the start's ask finds the platform away
    and the fetch waits for it. The program then starts with its secrets and
    is counted by the platform that answers now. The call itself is answered
    with a sentence, since its engine may have given it up meanwhile. The
    next call runs."""
    platform, engine = rig(listing=LISTING, credentials=[503, 200], start_status=[503, 200],
                           marker={"v": 1, "stamp": "1.0", "account": "a", "fetch": True, "label": "Fake"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 21, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    first = engine.answer(21)["result"]
    assert first["content"][0]["text"] == "OtoDock was restarting. Call the tool again." and first["isError"] is True
    engine.send({"jsonrpc": "2.0", "id": 22, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(22)["result"]["content"][0]["text"] == "ran echo"
    assert engine.starts() == ["start", "initialized"]
    assert len(platform.posts("start")) == 2
    engine.close()


def test_after_a_wait_only_a_held_call_is_answered_and_a_held_read_reaches_the_program(rig):
    """A read the saved answers do not cover, sent beside the call while the
    fetch waits, is held too. Only the call is answered with the sentence:
    the read goes on to the program, which answers it."""
    platform, engine = rig(listing=LISTING, credentials=[503, 200],
                           marker={"v": 1, "stamp": "1.0", "account": "a", "fetch": True, "label": "Fake"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 31, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    engine.send({"jsonrpc": "2.0", "id": 32, "method": "resources/read", "params": {"uri": "fake://one"}})
    call = engine.answer(31)
    assert call["result"]["content"][0]["text"] == "OtoDock was restarting. Call the tool again."
    read = engine.answer(32)
    assert read.get("result") == {} and "error" not in read
    engine.close()


def test_a_start_counted_at_the_first_ask_is_not_counted_again_after_a_wait(rig):
    """Each ask grows the session's reserve: a start the platform counted
    before the fetch waited is never asked again."""
    platform, engine = rig(listing=LISTING, credentials=[503, 200], start_status=[200],
                           marker={"v": 1, "stamp": "1.0", "account": "a", "fetch": True, "label": "Fake"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 33, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(33)["result"]["content"][0]["text"] == "OtoDock was restarting. Call the tool again."
    assert len(platform.posts("start")) == 1
    engine.close()


def test_a_refusal_at_the_second_ask_after_a_wait_stops_the_start(rig):
    """The first ask found the platform away and the fetch waited: the
    platform that answers now judges the start, and its refusal stops it."""
    sentence = "This chat has no room for another tool right now."
    platform, engine = rig(listing=LISTING, credentials=[503, 200], start_status=[503, 200],
                           ask={"ok": False, "message": sentence},
                           marker={"v": 1, "stamp": "1.0", "account": "a", "fetch": True, "label": "Fake"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 34, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(34)["result"]["content"][0]["text"] == sentence
    assert engine.starts() == [] and len(platform.posts("start")) == 2
    engine.close()


def test_a_tool_with_nothing_to_fetch_never_waits_for_the_platform(rig):
    """Its fetch has nothing to give: one ask, and the call runs as before."""
    platform, engine = rig(listing=LISTING, credentials=[503, 200])
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 23, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(23)["result"]["content"][0]["text"] == "ran echo"
    assert len(platform.posts("start")) == 1
    engine.close()


def test_the_programs_minute_runs_from_its_spawn_not_from_a_wait_for_the_platform(tmp_path, monkeypatch):
    """A fetch that waited for the platform (here 100 s) leaves the program
    its whole minute to start and answer."""
    program = tmp_path / "fake_program.py"
    program.write_text(FAKE_PROGRAM)
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "program.log"))
    monkeypatch.setenv("FAKE_TOOLS", json.dumps(TOOLS))
    offset = [0.0]
    real = time.monotonic
    monkeypatch.setattr(itc.time, "monotonic", lambda: real() + offset[0])
    monkeypatch.setattr(itc, "_route", lambda *a, **k: (200, {"ok": True}))

    def waited(child_env, *, hold):
        offset[0] += 100.0
        return True, True

    monkeypatch.setattr(itc, "_apply_broker_credentials", waited)
    out = open(os.devnull, "wb")
    stand_in = itc.StandIn([sys.executable, "-I", str(program)], [], "s" * 36,
                           {"v": 1, "label": "Fake", "fetch": True}, stdin=None, stdout=out, stderr=out)
    stand_in.listing = LISTING
    try:
        stand_in._start()
        assert stand_in.proc is not None and stand_in.proc.poll() is None and not stand_in.diverting
        assert stand_in.waited is True
    finally:
        if stand_in.proc is not None:
            stand_in.proc.kill()
            stand_in.proc.wait(timeout=10)
        out.close()


def test_a_changed_list_is_announced_to_the_engine_and_saved(rig):
    changed = [{**TOOLS[0], "description": "Echo, louder."}]
    platform, engine = rig(listing=LISTING, extra_env={"FAKE_TOOLS": json.dumps(changed)})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 14, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    engine.answer(14)
    engine.wait_for(lambda m: m.get("method") == "notifications/tools/list_changed")
    deadline = time.monotonic() + 5
    while not platform.posts("started") and time.monotonic() < deadline:
        time.sleep(0.05)
    saved = platform.posts("started")[0]["listing"]
    assert saved["lists"]["tools/list"][0]["description"] == "Echo, louder."
    # Re-listed after the notice, the engine reads the program's own list.
    engine.send({"jsonrpc": "2.0", "id": 15, "method": "tools/list"})
    assert engine.answer(15)["result"]["tools"][0]["description"] == "Echo, louder."
    engine.close()


def test_with_nothing_saved_the_program_starts_with_the_chat_and_its_answers_are_kept(rig):
    platform, engine = rig(listing=None)
    init = engine.handshake()["result"]
    assert init["serverInfo"]["name"] == "fake"
    assert init["protocolVersion"] == "2025-11-25"  # the program's own answer
    engine.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert engine.answer(2)["result"]["tools"][0]["name"] == "echo"
    # The engine's own initialized went nowhere: the program had one, its own.
    time.sleep(0.3)
    assert engine.starts() == ["start", "initialized"]
    assert platform.posts("start")[0]["with_session"] is True
    deadline = time.monotonic() + 5
    while not platform.posts("started") and time.monotonic() < deadline:
        time.sleep(0.05)
    kept = platform.posts("started")[0]
    assert kept["with_session"] is True and kept["listing"]["lists"]["tools/list"][0]["name"] == "echo"
    engine.close()


def test_a_report_is_sent_compact_and_in_utf8(rig):
    """``keep`` measures the compact text it stores: the report is sent the
    same way, so a list it accepts always fits the route's bound."""
    greek = [{**TOOLS[0], "description": "Ηχώ."}]
    platform, engine = rig(listing=None, extra_env={"FAKE_TOOLS": json.dumps(greek)})
    engine.handshake()
    deadline = time.monotonic() + 5
    while not platform.posts("started") and time.monotonic() < deadline:
        time.sleep(0.05)
    raw = next(r for r in platform.raw if b'"phase":"started"' in r)
    assert "Ηχώ.".encode() in raw and b'": ' not in raw and b"\\u" not in raw
    engine.close()


def _until(pred, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while not pred() and time.monotonic() < deadline:
        time.sleep(0.05)
    return pred()


WARM = {"v": 1, "stamp": "1.0", "account": "", "fetch": False, "label": "Fake", "warm": True}


def test_a_calls_tool_answers_at_once_and_starts_beside_the_engine(rig):
    """A call's tool (``warm``): the engine has its handshake and its list
    from the saved answers while the program, slow to start, starts with no
    request from the engine; a call meanwhile waits for that start and
    starts nothing more. The session counted it: no ``start`` is asked."""
    platform, engine = rig(listing=LISTING, marker=WARM, extra_env={"FAKE_SLOW": "2"})
    began = time.monotonic()
    assert engine.handshake()["result"]["instructions"] == "Use echo."
    engine.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert engine.answer(2)["result"]["tools"][0]["name"] == "echo"
    assert time.monotonic() - began < 1.5
    assert _until(lambda: engine.starts()[:1] == ["start"])
    engine.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(3)["result"]["content"][0]["text"] == "ran echo"
    assert engine.starts() == ["start", "initialized"]
    assert platform.posts("start") == []
    assert _until(lambda: bool(platform.posts("started")))
    started = platform.posts("started")[0]
    assert started["with_session"] is True and started["listing"] is None
    # A program up before the first call: that call paid no start.
    engine.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(4)["result"]["content"][0]["text"] == "ran echo"
    assert engine.starts().count("start") == 1
    engine.close()


def test_a_calls_tool_with_nothing_saved_starts_with_its_session_and_stays(rig):
    platform, engine = rig(listing=None, marker=WARM, kept=True)
    assert engine.handshake()["result"]["serverInfo"]["name"] == "fake"
    assert _until(lambda: bool(platform.posts("started")))
    time.sleep(0.6)
    assert platform.posts("start") == [] and platform.posts("stopped") == []
    assert "end" not in engine.starts()
    engine.close()


def test_a_ping_during_a_start_is_answered_at_once(rig):
    platform, engine = rig(listing=LISTING, extra_env={"FAKE_SLOW": "2"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert _until(lambda: engine.starts()[:1] == ["start"])
    engine.send({"jsonrpc": "2.0", "id": 6, "method": "ping"})
    engine.send({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert engine.answer(6)["result"] == {}
    assert engine.answer(7)["result"]["tools"][0]["name"] == "echo"
    assert not any(m.get("id") == 5 for m in engine.lines)  # the call still waits for its program
    assert engine.answer(5)["result"]["content"][0]["text"] == "ran echo"
    engine.close()


def test_a_cancel_and_a_log_level_sent_during_a_start_reach_the_program_after_its_call(rig):
    platform, engine = rig(listing=LISTING, extra_env={"FAKE_SLOW": "1"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert _until(lambda: engine.starts()[:1] == ["start"])
    engine.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 8}})
    engine.send({"jsonrpc": "2.0", "id": 9, "method": "logging/setLevel", "params": {"level": "debug"}})
    engine.answer(8)
    engine.answer(9)
    log = engine.starts()
    assert log.index("cancelled") < log.index("level debug")
    engine.close()


def test_a_program_started_for_its_answers_stops_once_they_are_kept(rig):
    """The first session after an update: nothing saved, every tool starts
    to list itself. Once the platform kept its answers and nothing called
    it, it stops, and the next call starts it again as any late start."""
    platform, engine = rig(listing=None, kept=True)
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert engine.answer(2)["result"]["tools"][0]["name"] == "echo"
    assert _until(lambda: engine.starts()[-1:] == ["end"])
    assert engine.starts() == ["start", "initialized", "end"]
    assert _until(lambda: len(platform.posts("stopped")) == 1)
    # The stand-in answers from them now, and starts nothing for a list.
    engine.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert engine.answer(3)["result"]["tools"][0]["name"] == "echo"
    engine.send({"jsonrpc": "2.0", "id": 4, "method": "ping"})
    assert engine.answer(4)["result"] == {}
    assert engine.starts() == ["start", "initialized", "end"]
    engine.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(5)["result"]["content"][0]["text"] == "ran echo"
    assert engine.starts()[3:5] == ["start", "initialized"]
    assert [b["with_session"] for b in platform.posts("start")] == [True, False]
    engine.close()


@pytest.mark.parametrize("kept", [False, None], ids=["not kept", "an older platform"])
def test_a_program_whose_answers_were_not_kept_stays(rig, kept):
    platform, engine = rig(listing=None, kept=kept)
    engine.handshake()
    assert _until(lambda: bool(platform.posts("started")))
    time.sleep(0.6)
    assert "end" not in engine.starts() and platform.posts("stopped") == []
    engine.close()


def test_a_program_the_engine_called_stays_for_the_session(rig):
    platform, engine = rig(listing=None, kept=True, report_delay=1.0)
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    assert engine.answer(6)["result"]["content"][0]["text"] == "ran echo"
    assert _until(lambda: bool(platform.posts("started")))
    time.sleep(1.5)
    assert "end" not in engine.starts() and platform.posts("stopped") == []
    engine.close()


@pytest.mark.parametrize("after", ["start", "engine"])
def test_a_program_whose_list_changed_stays(rig, after):
    """A program that says its list changed (a bridge that lists once it
    reached its server) no longer matches the answers kept at its start: it
    stays for the session, so a tool it added is never refused by name."""
    platform, engine = rig(listing=None, kept=True, report_delay=0.8, extra_env={"FAKE_CHANGED": after})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    engine.answer(2)
    assert _until(lambda: bool(platform.posts("started")))
    time.sleep(1.2)
    assert "end" not in engine.starts() and platform.posts("stopped") == []
    engine.close()


def test_a_kept_of_an_earlier_program_stops_nothing():
    """The platform's word on the keep names the program it was about (its
    generation): a word about an earlier one never stops the running one."""
    rfd, wfd = os.pipe()
    out = open(os.devnull, "wb")
    stand_in = itc.StandIn(["true"], [], "s" * 36, {"v": 1, "label": "Fake"},
                           stdin=os.fdopen(rfd, "rb"), stdout=out, stderr=out)
    program = subprocess.Popen([sys.executable, "-I", "-c", "import sys; sys.stdin.read()"],
                               stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
    stand_in.listing = stand_in.own = LISTING
    stand_in.proc, stand_in.generation, stand_in.tracking = program, 2, True
    runner = threading.Thread(target=stand_in.run, daemon=True)
    runner.start()
    try:
        stand_in.events.put(("kept", 1))
        time.sleep(0.5)
        assert program.poll() is None and stand_in.proc is program and stand_in.generation == 2
        stand_in.events.put(("kept", 2))
        program.wait(timeout=10)
        assert stand_in.proc is None and stand_in.generation == 3
    finally:
        os.close(wfd)
        runner.join(10)
        if program.poll() is None:
            program.kill()
        out.close()


def test_a_stop_waits_for_the_answer_in_flight(rig):
    """The keep arrives while the engine's list is still with the program:
    the engine gets its answer, then the program stops."""
    platform, engine = rig(listing=None, kept=True, extra_env={"FAKE_LIST_SLOW": "1.0"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    assert _until(lambda: bool(platform.posts("started")))
    assert "end" not in engine.starts()
    assert engine.answer(7)["result"]["tools"][0]["name"] == "echo"
    assert _until(lambda: engine.starts()[-1:] == ["end"])
    engine.close()


def test_a_log_level_reaches_the_program_after_it_started_again(rig):
    platform, engine = rig(listing=None, kept=True, report_delay=0.5)
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 8, "method": "logging/setLevel", "params": {"level": "debug"}})
    engine.answer(8)
    assert _until(lambda: engine.starts()[-1:] == ["end"])
    engine.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    engine.answer(9)
    assert engine.starts().count("level debug") == 2
    engine.close()


def test_an_answer_with_no_program_to_take_it_starts_nothing(rig):
    platform, engine = rig(listing=LISTING)
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": "r9", "result": {"roots": []}})
    engine.send({"jsonrpc": "2.0", "id": 10, "method": "ping"})
    engine.answer(10)
    assert engine.starts() == [] and platform.posts("start") == []
    engine.close()


def test_the_program_asks_for_its_roots_during_the_start_and_gets_them(rig):
    platform, engine = rig(listing=LISTING, extra_env={"FAKE_ASK_ROOTS": "1"})
    engine.handshake()
    engine.send({"jsonrpc": "2.0", "id": 16, "method": "tools/call", "params": {"name": "echo", "arguments": {}}})
    ask = engine.wait_for(lambda m: m.get("method") == "roots/list")
    engine.send({"jsonrpc": "2.0", "id": ask["id"], "result": {"roots": []}})
    assert engine.answer(16)["result"]["content"][0]["text"] == "ran echo"
    assert 'roots {"roots": []}' in engine.starts()
    engine.close()


def test_an_engine_that_leaves_before_any_call_leaves_nothing_running(rig):
    platform, engine = rig(listing=LISTING)
    engine.handshake()
    assert engine.close() == 0
    assert engine.starts() == []


# --- the fingerprint ---------------------------------------------------------

_SID = "0123abcd-0000-4000-8000-0000000000aa"
_BASE = {"OTO_ROLE": "editor", "OTO_SESSION_ID": _SID, "PROXY_API_KEY": "a", "PATH": "/usr/bin",
         "OTO_SSH_KEY_DIR": "/k/1", "MEETINGS_MCP_CHAT_ID": "c1", "CLAUDE_CODE_MESSAGING_SOCKET": "/s1",
         "OTO_WORKSPACE_DIR": f"/sessions/{_SID}/w", "SSH_AUTH_SOCK": "/a/0123abcd0000.sock"}


def _fp(env=None, argv=("python", "server.py"), stamp="1", account=""):
    return itc.listing_fingerprint(dict(env or _BASE), list(argv), stamp, account)


def test_the_fingerprint_repeats_across_two_sessions_of_one_identity():
    other_sid = "9999eeee-0000-4000-8000-0000000000bb"
    other = {**_BASE, "OTO_SESSION_ID": other_sid, "PROXY_API_KEY": "b", "OTO_SSH_KEY_DIR": "/k/2",
             "MEETINGS_MCP_CHAT_ID": "c2", "CLAUDE_CODE_MESSAGING_SOCKET": "/s2",
             "OTO_WORKSPACE_DIR": f"/sessions/{other_sid}/w", "SSH_AUTH_SOCK": "/a/9999eeee0000.sock",
             "SOME_TOKEN": "x"}
    assert _fp(other) == _fp()


def test_codexs_helper_folder_in_path_never_moves_the_fingerprint():
    """Codex puts a folder of its own, new per process, first in a tool's PATH."""
    one = {**_BASE, "PATH": "/u/.codex/tmp/arg0-linux/codex-arg0AbCdEf:/usr/bin"}
    two = {**_BASE, "PATH": "/u/.codex/tmp/arg0-linux/codex-arg0XyZ123:/usr/bin"}
    assert _fp(one) == _fp(two) == _fp({**_BASE, "PATH": "/usr/bin"})
    assert _fp({**_BASE, "PATH": "/opt/bin:/usr/bin"}) != _fp(one)


def test_the_marker_fingerprint_moves_when_the_tool_gains_its_secret():
    marker = {"v": 1, "stamp": "1", "account": "", "fetch": False, "label": "X"}
    without = itc.marker_fingerprint(marker, dict(_BASE), ["python", "server.py"])
    with_key = itc.marker_fingerprint({**marker, "fetch": True}, dict(_BASE), ["python", "server.py"])
    assert without != with_key
    assert without == itc.marker_fingerprint(dict(marker), dict(_BASE), ["python", "server.py"])


@pytest.mark.parametrize("change", [
    {"env": {**_BASE, "OTO_ROLE": "viewer"}},
    {"env": {**_BASE, "OTO_AVAILABLE_SCOPES": "user"}},
    {"argv": ("python", "server.py", "--readonly")},
    {"stamp": "2"},
    {"account": "another"},
])
def test_the_fingerprint_moves_with_what_shapes_a_list(change):
    assert _fp(**change) != _fp()


def test_the_stand_in_and_the_door_answer_from_one_function():
    """``saved_answer`` is the stand-in's own answers, lifted for a Docker
    tool's door: the stand-in keeps what it learns from them (the version it
    told, the engine's handshake, a log level to replay at the start)."""
    import io
    out = io.BytesIO()
    stand_in = itc.StandIn(["true"], [], "s" * 36, {"v": 1, "label": "Fake"},
                           stdin=io.BytesIO(), stdout=out, stderr=io.BytesIO())
    stand_in.listing = LISTING
    level = {"jsonrpc": "2.0", "id": 4, "method": "logging/setLevel", "params": {"level": "debug"}}
    init = {"jsonrpc": "2.0", "id": 5, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
    assert stand_in._answer(level) and stand_in._answer(init)
    assert stand_in.set_level == {"level": "debug"}
    assert stand_in.init_msg == init and stand_in.told
    sent = [json.loads(line) for line in out.getvalue().splitlines()]
    assert sent == [itc.saved_answer(LISTING, level, "Fake"), itc.saved_answer(LISTING, init, "Fake")]
    assert itc.saved_answer(LISTING, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                                      "params": {"name": LISTING["lists"]["tools/list"][0]["name"]}}, "Fake") is None
