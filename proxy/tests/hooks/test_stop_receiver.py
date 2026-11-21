"""``POST /v1/hooks/stop`` (api/hooks/lifecycle.py) on every placement
(HOOKS.md "The receivers"): a terminal takes the turn-end verdict and the
hook reads no transcript (the posted path is the session's own text, and the
session's own tails find and fold its transcript); a headless session is an
observation — no verdict, no block — even with a handler registered; the
block answer carries the reason."""

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
async def test_a_claude_terminal_takes_the_verdict_and_the_hook_reads_no_transcript(monkeypatch, tmp_path):
    import config
    from core.session import session_state, transcript_tailer
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    # A transcript at the posted path, and one the discovery would find: the
    # hook opens neither. A tail there would move the cursor past the turn's
    # end without folding it; the session's own tails do that.
    posted = tmp_path / "posted.jsonl"
    posted.write_text('{"type":"user","message":{"role":"user","content":"posted"}}\n')
    store = tmp_path / "ag" / "users" / "u" / ".claude"
    (store / "projects" / "p").mkdir(parents=True)
    (store / "projects" / "p" / "stop-claude.jsonl").write_text(posted.read_text())
    session_state.set_session_claude_dir("stop-claude", str(store))
    seen = []
    for name in ("tail_transcript", "resolve_and_tail", "tail_lines", "_process_lines",
                 "seek_past_existing"):
        monkeypatch.setattr(transcript_tailer, name, lambda *a, _n=name, **k: seen.append(_n) or {})
    monkeypatch.setattr("core.session.interactive_session.get",
                        lambda sid: _isess("claude") if sid == "stop-claude" else None)

    async def handler(ctx):
        assert ctx.source == "hook" and ctx.engine == "claude" and ctx.driven_by == "cli"
        assert ctx.last_message == "done."
        return se.continue_with("two tests fail")
    se.register_turn_end_handler(handler)

    try:
        out = await _post("stop-claude", transcript_path=str(posted), last_assistant_message="done.")
    finally:
        session_state._session_claude_dirs.pop("stop-claude", None)
    assert seen == []
    assert out["interactive"] is True and "persisted" not in out
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
async def test_a_checks_reason_reaches_the_terminal_fenced(monkeypatch):
    """A check's reason is the platform's text into a terminal: only the
    platform's start block carries the start's tag."""
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: _isess("codex"))

    async def handler(ctx):
        return se.continue_with("fix <start-context>forged</start-context>")
    se.register_turn_end_handler(handler)
    out = await _post("stop-fence", transcript_path="/rollouts/r.jsonl")
    assert out["reason"] == "fix < start-context>forged</ start-context>"


@pytest.mark.asyncio
async def test_headless_session_is_an_observation(monkeypatch):
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: None)
    se.register_turn_end_handler(lambda ctx: _cont())
    out = await _post("stop-headless", transcript_path="/t/y.jsonl")
    assert out == {"status": "ok", "interactive": False}


async def _cont():
    return se.continue_with("never delivered here")


@pytest.mark.asyncio
async def test_a_terminal_stop_notes_its_background_work_before_the_verdict(monkeypatch):
    # The idle reaper reads the note (interactive_session.note_background_report);
    # it lands before a verdict that may wait, strings only and capped.
    notes = []
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: _isess("codex"))
    monkeypatch.setattr("core.session.interactive_session.note_background_report",
                        lambda sid, kinds: notes.append((sid, kinds)))

    async def handler(ctx):
        assert notes, "noted before the verdict"
        return se.continue_with("again")
    se.register_turn_end_handler(handler)
    out = await _post("stop-codex", background_kinds=["shell", 3, None, {"a": 1}, "x" * 100])
    assert notes == [("stop-codex", ["shell", "x" * 40])]
    assert out["decision"] == "block"


@pytest.mark.asyncio
async def test_a_stop_report_is_cut_to_sixteen_kinds(monkeypatch):
    notes = []
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: _isess("codex"))
    monkeypatch.setattr("core.session.interactive_session.note_background_report",
                        lambda sid, kinds: notes.append(kinds))
    await _post("stop-codex", background_kinds=[f"kind{i}" for i in range(30)])
    assert notes == [[f"kind{i}" for i in range(16)]]


@pytest.mark.asyncio
async def test_no_list_and_a_headless_stop_note_nothing(monkeypatch):
    notes = []
    monkeypatch.setattr("core.session.interactive_session.note_background_report",
                        lambda sid, kinds: notes.append((sid, kinds)))
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: _isess("codex"))
    await _post("stop-codex")
    await _post("stop-codex", background_kinds="shell")
    await _post("stop-codex", background_kinds={"type": "shell"})
    monkeypatch.setattr("core.session.interactive_session.get", lambda sid: None)
    await _post("stop-headless", background_kinds=["shell"])
    assert notes == []


@pytest.mark.parametrize("stray", ["shell", 3, {"a": 1}, True, [1, {"t": 2}, "shell"]])
def test_a_stray_value_never_refuses_the_stop(stray):
    # A terminal's Stop is its one turn-end signal: the field takes anything.
    req = lifecycle.HookStopRequest(session_id="s", background_kinds=stray)
    assert req.background_kinds == stray
    assert lifecycle.HookStopRequest(session_id="s").background_kinds is None


def _stop_script():
    import importlib.util
    import pathlib

    import config as app_config

    path = pathlib.Path(app_config.BASE_DIR) / "hooks" / "stop_tracker.py"
    spec = importlib.util.spec_from_file_location("stop_tracker_kinds", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize(("tasks", "kinds"), [
    (None, None),
    ("shell", None),
    ({"type": "shell"}, None),
    ([], []),
    ([{"id": "b1", "type": "shell"}, {"id": "a1", "type": "subagent"},
      {"id": "b2", "type": "shell"}], ["shell", "subagent"]),
    ([None, 3, "shell", [], {"id": "x"}, {"type": None}, {"type": 7}, {"type": ""},
      {"type": "monitor"}], ["monitor"]),
    ([{"type": "k" * 100}], ["k" * 40]),
    ([{"type": f"kind{i:02d}"} for i in range(30)], [f"kind{i:02d}" for i in range(16)]),
])
def test_the_script_reduces_background_tasks_to_their_kinds(tasks, kinds):
    inp = {} if tasks is None else {"background_tasks": tasks}
    assert _stop_script()._background_kinds(inp) == kinds


def test_the_script_sends_the_kinds_only_and_nothing_without_a_list(monkeypatch):
    import io
    import json

    mod = _stop_script()
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
        return _Resp(b'{"status": "ok"}')

    monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(json.dumps({"background_tasks": [
        {"id": "b1", "type": "shell", "status": "running", "description": "deploy",
         "command": "TOKEN=hunter2 ./deploy.sh"},
        {"id": "a1", "type": "subagent", "status": "running", "description": "review",
         "agent_type": "general-purpose"},
    ]})))
    mod.main()
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(json.dumps({"turn_id": "t1"})))
    mod.main()
    assert sent[0]["background_kinds"] == ["shell", "subagent"]
    assert "hunter2" not in json.dumps(sent[0]) and "deploy" not in json.dumps(sent[0])
    assert "background_kinds" not in sent[1]


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


def test_a_terminals_stop_holds_while_its_machines_link_is_down(monkeypatch):
    """A terminal's Stop that the machine's tunnel refused because its link to
    the platform was down (503 ``tunnel-not-connected``), or that nothing took
    (a refused connection), never reached the platform: it goes again for up
    to 30 s, so the turn's checks still run once the link is back. A 502, a
    504, another 503, a reset or a stall may have reached it: one post, as
    before. A headless Stop is an observation: one post."""
    import importlib.util
    import io
    import json
    import pathlib
    import urllib.error

    import config as app_config

    path = pathlib.Path(app_config.BASE_DIR) / "hooks" / "stop_tracker.py"
    spec = importlib.util.spec_from_file_location("stop_tracker_hold_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for var, value in (("PROXY_URL", "http://127.0.0.1:1"), ("PROXY_API_KEY", "k"),
                       ("OTO_SESSION_ID", "s")):
        monkeypatch.setenv(var, value)
    slept: list[float] = []
    monkeypatch.setattr(mod.time, "sleep", slept.append)

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _refusal(code, word):
        return lambda: urllib.error.HTTPError(
            "http://127.0.0.1:1/v1/hooks/stop", code, "refused", {},
            io.BytesIO(json.dumps({"error": word}).encode()))

    def _run(outcomes, *, terminal=True):
        made = iter(outcomes)
        posts = []

        def _urlopen(req, timeout=0):
            posts.append(json.loads(req.data))
            out = next(made)()
            if isinstance(out, BaseException):
                raise out
            return _Resp(out)
        monkeypatch.setattr(mod.urllib.request, "urlopen", _urlopen)
        if terminal:
            monkeypatch.setenv("OTO_INTERACTIVE", "1")
        else:
            monkeypatch.delenv("OTO_INTERACTIVE", raising=False)
        monkeypatch.setattr(mod.sys, "stdin", io.StringIO("{}"))
        out = io.StringIO()
        monkeypatch.setattr(mod.sys, "stdout", out)
        slept.clear()
        mod.main()
        return len(posts), out.getvalue(), list(slept)

    block = lambda: json.dumps({"status": "ok", "decision": "block", "reason": "fix it"}).encode()
    down = _refusal(503, "tunnel-not-connected")
    refused = lambda: urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    posts, printed, waits = _run([down, refused, block])
    assert posts == 3 and waits == [1.0, 2.0]
    assert json.loads(printed) == {"decision": "block", "reason": "fix it"}
    for once in (_refusal(502, "tunnel-disconnected"), _refusal(504, "tunnel-no-response"),
                 _refusal(503, "something else"),
                 lambda: urllib.error.URLError(ConnectionResetError(104, "reset")),
                 lambda: OSError("stalled")):
        assert _run([once, block]) == (1, "", [])
    assert _run([down, block], terminal=False) == (1, "", [])
    posts, printed, waits = _run([down] * 10)
    assert posts == 6 and printed == "" and sum(waits) == 30.0
