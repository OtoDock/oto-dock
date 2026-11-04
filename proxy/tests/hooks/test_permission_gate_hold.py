"""The permission gate holds a call while the platform cannot be reached.

A proxy restart or a dropped tunnel answers the hook with a connection error
or the satellite tunnel's synthetic 502/503: the gate retries for up to its
hold (120 s) and then denies ("platform unreachable"); any answer from the
platform ends the hold at once. A refused session token gets a short ladder
(the satellite reports its sessions a moment after it reconnects); any other
refusal denies at once. A streamed answer is whitespace keepalives and the
JSON last; a body that ends before its JSON is a dropped stream.
"""

from __future__ import annotations

import importlib.util
import io
import urllib.error

import pytest

from tests._paths import PROXY_DIR


@pytest.fixture
def gate(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "permission_gate_under_test", PROXY_DIR / "hooks" / "permission_gate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    clock = {"t": 1000.0}
    slept: list[float] = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    monkeypatch.setattr(mod.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(mod.time, "sleep", sleep)
    mod._slept = slept
    return mod


class _Resp:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _answers(gate, monkeypatch, outcomes):
    calls = iter(outcomes)

    def urlopen(req, timeout=0):
        out = next(calls)
        if isinstance(out, Exception):
            raise out
        return _Resp(out)

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)


def _http(code):
    return urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(b""))


def test_an_unreachable_platform_is_held_until_it_answers(gate, monkeypatch):
    _answers(gate, monkeypatch, [
        urllib.error.URLError("refused"), _http(503), _http(502),
        b'{"decision": "allow"}',
    ])
    assert gate._request_decision(object()) == {"decision": "allow"}
    assert gate._slept == [2.0, 4.0, 8.0]


def test_a_crashed_gate_denies_the_call(tmp_path):
    """The script's own crash prints a deny and exits 0: both CLIs read a
    non-zero exit as "continue", and Claude Code's onFailure block cannot see
    an exit 0 with no output. A JSON array on stdin is a payload main cannot
    read (``inp.get``), the breadcrumb lands in OTO_HOOK_LOG."""
    import json
    import os
    import subprocess
    import sys

    log = tmp_path / "hook-error.log"
    env = {**os.environ, "PROXY_URL": "http://127.0.0.1:1", "PROXY_API_KEY": "k",
           "OTO_HOOK_LOG": str(log)}
    out = subprocess.run(
        [sys.executable, "-I", str(PROXY_DIR / "hooks" / "permission_gate.py")],
        input="[]", capture_output=True, text=True, env=env, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    decision = json.loads(out.stdout)["hookSpecificOutput"]
    assert decision["permissionDecision"] == "deny"
    assert "gate error" in decision["permissionDecisionReason"]
    assert "AttributeError" in log.read_text()


def test_the_hold_ends_after_120_seconds_with_none(gate, monkeypatch):
    _answers(gate, monkeypatch, [_http(503)] * 50)
    assert gate._request_decision(object()) is None
    assert sum(gate._slept) <= gate._HOLD_S


def test_a_refused_token_gets_a_short_ladder_then_a_deny(gate, monkeypatch):
    _answers(gate, monkeypatch, [_http(401)] * 4)
    out = gate._request_decision(object())
    assert out["decision"] == "deny" and "401" in out["reason"]
    assert gate._slept == [2.0, 4.0, 8.0]


def test_any_other_refusal_denies_at_once(gate, monkeypatch):
    _answers(gate, monkeypatch, [_http(403)])
    out = gate._request_decision(object())
    assert out["decision"] == "deny" and "403" in out["reason"]
    assert gate._slept == []


def test_a_chunked_answer_cut_mid_body_is_a_dropped_stream(gate, monkeypatch):
    """A local prompt's streamed answer cut by a dying proxy raises
    IncompleteRead in urllib: held like any dropped stream, never a crash
    (a crashed hook exits 0 and the tool runs)."""
    import http.client
    _answers(gate, monkeypatch, [http.client.IncompleteRead(b"   "), b'{"decision": "allow"}'])
    assert gate._request_decision(object()) == {"decision": "allow"}
    assert gate._slept == [2.0]


def test_a_streamed_answer_and_a_dropped_stream(gate, monkeypatch):
    _answers(gate, monkeypatch, [b"   ", b'  \n {"decision": "deny", "reason": "no"}'])
    assert gate._request_decision(object()) == {"decision": "deny", "reason": "no"}
    assert gate._slept == [2.0]  # the keepalive-only body was a dropped stream


def test_the_hold_counts_from_the_first_failure(gate, monkeypatch):
    """A prompt that waited ten minutes before its stream dropped is still
    held and retried: the hold starts at the failure, not the first call."""
    clock_box = {}

    def urlopen(req, timeout=0):
        n = clock_box.setdefault("n", 0)
        clock_box["n"] = n + 1
        if n == 0:
            gate.time.sleep(600.0)  # the person took ten minutes
            raise urllib.error.URLError("reset")
        return _Resp(b'{"decision": "allow"}')

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    assert gate._request_decision(object()) == {"decision": "allow"}
    assert gate._slept == [600.0, 2.0]


def test_the_gate_stamps_its_call_with_this_hosts_clock(gate, monkeypatch):
    """The body carries ``hook_sent_at`` (the gate host's wall clock, the one
    the CLI's transcript lines carry): a terminal parked on the dialog this
    call opens ignores lines journaled before it."""
    import json
    sent = []

    def urlopen(req, timeout=0):
        sent.append(json.loads(req.data))
        return _Resp(b'{"decision": "allow"}')

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(gate.time, "time", lambda: 1234.5)
    monkeypatch.setenv("OTO_SESSION_ID", "s1")
    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("PROXY_API_KEY", "k")
    monkeypatch.setattr(gate.sys, "stdin", io.StringIO(json.dumps(
        {"tool_name": "AskUserQuestion", "tool_input": {}, "permission_mode": "default"})))
    monkeypatch.setattr(gate.sys, "stdout", io.StringIO())
    gate.main()
    assert sent and sent[0]["hook_sent_at"] == 1234.5 and sent[0]["tool_name"] == "AskUserQuestion"


def test_the_gate_forwards_the_calls_id(gate, monkeypatch):
    """The body carries the hook input's ``tool_use_id``: a call the gate
    allows in Plan is recorded under it for the CLI's own ask that follows
    (Claude Code 2.1.299+); an input without one sends an empty id."""
    import json
    sent = []

    def urlopen(req, timeout=0):
        sent.append(json.loads(req.data))
        return _Resp(b'{"decision": "allow"}')

    monkeypatch.setattr(gate.urllib.request, "urlopen", urlopen)
    monkeypatch.setenv("OTO_SESSION_ID", "s1")
    monkeypatch.setenv("PROXY_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("PROXY_API_KEY", "k")
    for inp in ({"tool_name": "Bash", "tool_input": {}, "tool_use_id": "toolu_1"},
                {"tool_name": "Bash", "tool_input": {}}):
        monkeypatch.setattr(gate.sys, "stdin", io.StringIO(json.dumps(inp)))
        monkeypatch.setattr(gate.sys, "stdout", io.StringIO())
        gate.main()
    assert [s["tool_use_id"] for s in sent] == ["toolu_1", ""]


def _http_body(code, body: bytes):
    return urllib.error.HTTPError("http://x", code, "x", {}, io.BytesIO(body))


@pytest.mark.parametrize("failure, words", [
    (lambda: urllib.error.URLError(ConnectionRefusedError(111, "Connection refused")),
     "no answer at 127.0.0.1:57353: connection refused"),
    (lambda: urllib.error.URLError(TimeoutError("timed out")), "no answer at 127.0.0.1:57353: timed out"),
    (lambda: _http_body(503, b'{"error": "tunnel-not-connected"}'),
     "this machine's link to OtoDock was down: 503"),
    (lambda: _http_body(502, b'{"error": "tunnel-disconnected"}'),
     "this machine's link to OtoDock dropped: 502"),
    (lambda: _http_body(502, b'{"detail": "bad gateway"}'), "502 from the platform"),
    (lambda: b"   ", "the platform's answer was cut off"),
], ids=["refused", "timeout", "tunnel-503", "tunnel-502", "platform-502", "cut-off"])
def test_the_hold_keeps_its_last_failure_in_words(gate, monkeypatch, failure, words):
    gate._where = "127.0.0.1:57353"
    _answers(gate, monkeypatch, [failure() for _ in range(50)])
    assert gate._request_decision(object()) is None
    assert gate._last_failure == words
    assert gate._held_s == sum(gate._slept)


def test_the_deny_names_the_failure_its_address_and_the_hold(gate, monkeypatch, capsys):
    """The reason keeps its prefix and says which link failed: the field
    incident read "platform unreachable" for hours with nothing listening
    on the machine's tunnel port."""
    import json
    _answers(gate, monkeypatch,
             [urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))] * 50)
    for name, value in (("PROXY_URL", "http://127.0.0.1:57353"), ("PROXY_API_KEY", "k"),
                        ("OTO_SESSION_ID", "s")):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("OTO_HOOK_BRIDGED", raising=False)
    monkeypatch.delenv("OTO_HOOK_DENY_ONLY", raising=False)
    monkeypatch.setattr(gate.sys, "stdin", io.StringIO('{"tool_name": "Bash", "tool_input": {}}'))
    gate.main()
    out = json.loads(capsys.readouterr().out)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    assert out["permissionDecisionReason"] == (
        "OtoDock platform unreachable (no answer at 127.0.0.1:57353: connection refused "
        f"for {sum(gate._slept):.0f} s). Tool call denied (fail closed).")
