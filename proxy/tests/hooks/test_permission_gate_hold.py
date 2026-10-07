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
