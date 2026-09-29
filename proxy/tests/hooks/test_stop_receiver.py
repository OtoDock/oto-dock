"""``POST /v1/hooks/stop`` (api/hooks/lifecycle.py) on every placement
(HOOKS.md "The receivers"): a Claude terminal tails its transcript and takes
the turn-end verdict; a Codex terminal takes the verdict without a tail; a
headless session is an observation — no verdict, no block — even with a
handler registered; the block answer carries the reason."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.hooks import lifecycle
from core.session import session_events as se


@pytest.fixture(autouse=True)
def _iso(monkeypatch):
    async def _pass(auth, sid):
        return None

    monkeypatch.setattr(lifecycle, "verify_session_match_async", _pass)
    handlers = list(se._turn_end_handlers)
    yield
    for h in list(se._turn_end_handlers):
        if h not in handlers:
            se.unregister_turn_end_handler(h)
    for sid in ("stop-claude", "stop-codex", "stop-headless"):
        se.cleanup_session(sid)


def _isess(kind: str):
    # The session's tailer is its engine's (``InteractiveSession._tailer``):
    # the Claude tailer takes the hook's transcript pointer, the Codex
    # rollout tailer reads on its own — what the receiver now asks.
    from core.session import codex_rollout_tailer, transcript_tailer
    tailer = transcript_tailer if kind == "claude" else codex_rollout_tailer
    return SimpleNamespace(transcript_kind=kind, chat_id="chat-1", _tailer=lambda: tailer)


async def _post(session_id: str, **fields):
    req = lifecycle.HookStopRequest(session_id=session_id, **fields)
    return await lifecycle.hook_stop(req, authorization="Bearer x")


@pytest.mark.asyncio
async def test_claude_terminal_tails_and_answers_the_verdict(monkeypatch):
    tails = []
    monkeypatch.setattr("core.session.interactive_session.get",
                        lambda sid: _isess("claude") if sid == "stop-claude" else None)
    monkeypatch.setattr(
        "core.session.transcript_tailer.tail_transcript",
        lambda sid, chat_id, path: tails.append((sid, chat_id, path)) or {"persisted": 2},
    )

    async def handler(ctx):
        assert ctx.source == "hook" and ctx.engine == "claude" and ctx.driven_by == "cli"
        assert ctx.last_message == "done."
        return se.continue_with("two tests fail")
    se.register_turn_end_handler(handler)

    out = await _post("stop-claude", transcript_path="/t/x.jsonl", last_assistant_message="done.")
    assert tails == [("stop-claude", "chat-1", "/t/x.jsonl")]
    assert out["interactive"] is True and out["persisted"] == 2
    assert out["decision"] == "block" and out["reason"] == "two tests fail"


@pytest.mark.asyncio
async def test_codex_terminal_takes_the_verdict_without_a_transcript_tail(monkeypatch):
    monkeypatch.setattr("core.session.interactive_session.get",
                        lambda sid: _isess("codex"))
    calls = []
    monkeypatch.setattr("core.session.transcript_tailer.tail_transcript",
                        lambda *a: calls.append(a) or {})

    async def handler(ctx):
        assert ctx.engine == "codex"
        return se.continue_with("lint")
    se.register_turn_end_handler(handler)
    out = await _post("stop-codex", transcript_path="/rollouts/r.jsonl")
    assert calls == []          # a rollout is not a Claude transcript
    assert out["decision"] == "block" and out["reason"] == "lint"


@pytest.mark.asyncio
async def test_headless_session_is_an_observation(monkeypatch):
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: None)
    se.register_turn_end_handler(lambda ctx: _cont())
    out = await _post("stop-headless", transcript_path="/t/y.jsonl")
    assert out == {"status": "ok", "interactive": False}


async def _cont():
    return se.continue_with("never delivered here")


@pytest.mark.asyncio
async def test_no_verdict_means_no_decision_field(monkeypatch):
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: _isess("codex"))
    out = await _post("stop-codex")
    assert "decision" not in out and out["interactive"] is True


def test_the_stop_script_prints_the_block_shape_only_on_a_block(monkeypatch, tmp_path):
    """The script side of the same contract: ``{"decision": "block",
    "reason"}`` on stdout when the proxy says so — the one shape both
    engines accept on Stop — and nothing otherwise, nothing on a dead proxy."""
    import importlib.util
    import io
    import json
    import pathlib
    import config as app_config

    path = pathlib.Path(app_config.BASE_DIR) / "hooks" / "stop_tracker.py"
    spec = importlib.util.spec_from_file_location("stop_tracker_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for var, value in (("PROXY_URL", "http://127.0.0.1:1"), ("PROXY_API_KEY", "k"),
                       ("OTO_SESSION_ID", "s")):
        monkeypatch.setenv(var, value)

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    sent = []

    def _urlopen(req, timeout=0):
        sent.append(json.loads(req.data))
        return _Resp(json.dumps({"status": "ok", "decision": "block", "reason": "fix it"}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(json.dumps({
        "transcript_path": "/t/z.jsonl", "stop_hook_active": True,
        "last_assistant_message": "m" * 20000,
    })))
    out = io.StringIO()
    monkeypatch.setattr(mod.sys, "stdout", out)
    mod.main()
    assert json.loads(out.getvalue()) == {"decision": "block", "reason": "fix it"}
    assert sent[0]["stop_hook_active"] is True
    assert len(sent[0]["last_assistant_message"]) == mod._LAST_MESSAGE_MAX

    # No block → nothing printed.
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda req, timeout=0: _Resp(b'{"status":"ok"}'))
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(mod.sys, "stdout", out)
    mod.main()
    assert out.getvalue() == ""

    # Dead proxy → fail open, nothing printed.
    def _boom(req, timeout=0):
        raise OSError("down")
    monkeypatch.setattr(mod.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO("{}"))
    out = io.StringIO()
    monkeypatch.setattr(mod.sys, "stdout", out)
    mod.main()
    assert out.getvalue() == ""


def test_the_stop_script_waits_for_a_verdict_only_on_a_terminal(monkeypatch):
    """A terminal (``OTO_INTERACTIVE``) may wait on a check's verdict; a
    proxy-driven session's Stop is an observation, bounded so a stalled
    proxy connection never holds its turn end."""
    import importlib.util
    import io
    import json
    import pathlib
    import config as app_config

    path = pathlib.Path(app_config.BASE_DIR) / "hooks" / "stop_tracker.py"
    spec = importlib.util.spec_from_file_location("stop_tracker_timeout_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for var, value in (("PROXY_URL", "http://127.0.0.1:1"), ("PROXY_API_KEY", "k"),
                       ("OTO_SESSION_ID", "s")):
        monkeypatch.setenv(var, value)
    timeouts = []

    def _urlopen(req, timeout=0):
        timeouts.append(timeout)
        raise OSError("stalled")

    monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(mod.sys, "stdout", io.StringIO())
    monkeypatch.delenv("OTO_INTERACTIVE", raising=False)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(json.dumps({})))
    mod.main()
    monkeypatch.setenv("OTO_INTERACTIVE", "1")
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(json.dumps({})))
    mod.main()
    assert timeouts == [10, 604800]
