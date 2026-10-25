"""The Codex hook floor for unattended sessions (task / phone / meeting /
trigger / internal): thread-level hook trust on thread/start, the deny-only
+ no-forward environment, and the PostToolUse forwarder staying quiet
(docs/execution-layers/CODEX.md, "Hook floor")."""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib

import pytest

import config as app_config
from core.layers.codex import session as codex_session
from core.layers.codex.session import CodexAppServerSession
from core.session import session_kind


def _session(**kw) -> CodexAppServerSession:
    return CodexAppServerSession(**{
        "session_id": "11111111-2222-4333-8444-555555555555", "agent_name": "support",
        "model": "gpt-6", "sandbox_mode": "danger-full-access", "working_dir": "",
        "config_dir": "/tmp/codex-home", **kw,
    })


def test_the_unattended_kinds_are_the_no_human_set():
    unattended = {k.name for k in session_kind.KINDS if not k.attended}
    assert unattended == {"task", "phone", "meeting", "trigger", "internal", "app"}
    assert session_kind.attended("dashboard") and session_kind.attended("")


def test_every_thread_trusts_the_hook():
    """The trust is fixed per thread while the mode moves per turn, so an
    attended chat trusts its hook too, and the hook judges every call."""
    attended = _session(permission_mode="dontAsk")._thread_overrides()
    assert attended["config"] == {"bypass_hook_trust": True}
    assert attended["approvalPolicy"] == "never" and attended["approvalsReviewer"] == "user"
    asking = _session(permission_mode="ask", sandbox_mode="workspace-write")._thread_overrides()
    assert asking["config"] == {"bypass_hook_trust": True}
    assert (asking["approvalPolicy"], asking["approvalsReviewer"]) == ("on-request", "user")
    reviewed = _session(permission_mode="autoReview", sandbox_mode="workspace-write")._thread_overrides()
    assert (reviewed["approvalPolicy"], reviewed["approvalsReviewer"]) == ("on-request", "auto_review")
    floored = _session(hooks_floor=True, permission_mode="auto")._thread_overrides()
    assert floored["config"] == {"bypass_hook_trust": True}
    assert floored["approvalPolicy"] == "never"


def test_daemon_env_marks_deny_only_and_no_forward(monkeypatch):
    """The hook judges every call of every session: no session is marked as
    bridged (the gate honours the mark only from a satellite before 1.9),
    and a stray value from the session's own env never reaches one."""
    from core.sandbox import env_builder
    monkeypatch.setattr(env_builder, "build_session_env", lambda *a, **k: {"PATH": "/usr/bin"})
    monkeypatch.setattr(app_config, "CODEX_BIN", "/usr/bin/codex", raising=False)
    for kw in ({"hooks_floor": True}, {}):
        env = _session(**kw)._build_env()
        assert env["OTO_HOOK_DENY_ONLY"] == "1" and env["OTO_HOOK_NO_FORWARD"] == "1"
        assert "OTO_HOOK_BRIDGED" not in env
        env = _session(**kw, extra_env={"OTO_HOOK_BRIDGED": "1"})._build_env()
        assert "OTO_HOOK_BRIDGED" not in env


def test_the_daemon_env_carries_a_command_line_token_over_the_final_session_token(monkeypatch):
    """Codex's shell inherits the daemon's env: the token mirrors the
    session token the daemon ends up with (the session's own env, applied
    after the builder's, may replace it)."""
    from auth.session_token import create_session_token, validate_cli_token
    from core.sandbox import env_builder
    sid = "11111111-2222-4333-8444-555555555555"
    monkeypatch.setattr(env_builder, "build_session_env", lambda *a, **k: {
        "PATH": "/usr/bin", "PROXY_API_KEY": create_session_token(sid, "support", "")})
    monkeypatch.setattr(app_config, "CODEX_BIN", "/usr/bin/codex", raising=False)
    final = create_session_token(sid, "support", "user-x")
    env = _session(extra_env={"PROXY_API_KEY": final})._build_env()
    assert validate_cli_token(env["OTO_CLI_TOKEN"])["user_sub"] == "user-x"


def test_the_terminal_env_carries_a_command_line_token_over_the_final_session_token(monkeypatch):
    """The bare Codex TUI's shell inherits ``_tui_env``: the token mirrors
    the session token the subscription env leaves last."""
    from auth.session_token import create_session_token, validate_cli_token
    from core.layers.codex import layer as codex_layer
    from core.sandbox import env_builder
    sid = "11111111-2222-4333-8444-555555555557"
    monkeypatch.setattr(env_builder, "build_session_env", lambda *a, **k: {
        "PATH": "/usr/bin", "PROXY_API_KEY": create_session_token(sid, "support", "")})
    final = create_session_token(sid, "support", "user-x")
    env = codex_layer._tui_env(sid, "support", "", "", "/usr/bin/codex",
                               {"PROXY_API_KEY": final, "CODEX_HOME": "/tmp/codex-home"})
    cli = validate_cli_token(env["OTO_CLI_TOKEN"])
    assert cli["sid"] == sid and cli["user_sub"] == "user-x"
    assert env["OTO_INTERACTIVE"] == "1" and env["OTO_HOOK_DENY_ONLY"] == "1"


@pytest.mark.asyncio
async def test_create_codex_session_threads_the_flag(monkeypatch):
    seen: dict = {}

    async def _start(self):
        seen["hooks_floor"] = self.hooks_floor
    monkeypatch.setattr(CodexAppServerSession, "start", _start)
    s = await codex_session.create_codex_session(
        "11111111-2222-4333-8444-555555555556", "support", "gpt-6",
        config_dir="/tmp/codex-home", hooks_floor=True, thread_cwd="/users/alice",
    )
    try:
        assert seen == {"hooks_floor": True} and s.hooks_floor is True
        assert s.thread_cwd == "/users/alice" and s.working_dir == ""
    finally:
        await codex_session.close_codex_session(s.session_id)


def _forwarder_module():
    path = pathlib.Path(app_config.BASE_DIR) / "hooks" / "tool_result_forwarder.py"
    spec = importlib.util.spec_from_file_location("tool_result_forwarder_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_forwarder_stays_quiet_under_the_hook_floor(monkeypatch):
    mod = _forwarder_module()
    calls: list = []
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: calls.append(a) or io.BytesIO(b"{}"))
    for var, value in (("PROXY_URL", "http://127.0.0.1:1"), ("PROXY_API_KEY", "k"),
                       ("OTO_SESSION_ID", "s"), ("OTO_HOOK_NO_FORWARD", "1")):
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("OTO_INTERACTIVE", raising=False)
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(json.dumps({
        "tool_name": "mcp__memory-mcp__memory", "tool_response": {"content": "ok"},
    })))
    mod.main()
    assert calls == []
    # Without the flag the headless path forwards as before.
    monkeypatch.delenv("OTO_HOOK_NO_FORWARD")
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(json.dumps({
        "tool_name": "mcp__memory-mcp__memory", "tool_response": {"content": "ok"},
    })))
    mod.main()
    assert len(calls) == 1


def test_subagent_tracker_stays_quiet_on_app_server_threads(monkeypatch):
    # The thread router owns a Codex app-server session's sub-agents; a
    # SubagentStop at a child turn end would park a reused child as finished.
    path = pathlib.Path(app_config.BASE_DIR) / "hooks" / "subagent_tracker.py"
    spec = importlib.util.spec_from_file_location("subagent_tracker_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls: list = []
    monkeypatch.setattr(mod.urllib.request, "urlopen", lambda *a, **k: calls.append(a) or io.BytesIO(b"{}"))
    for var, value in (("PROXY_URL", "http://127.0.0.1:1"), ("PROXY_API_KEY", "k"),
                       ("OTO_SESSION_ID", "s"), ("OTO_HOOK_NO_FORWARD", "1")):
        monkeypatch.setenv(var, value)
    monkeypatch.delenv("OTO_INTERACTIVE", raising=False)
    stop = json.dumps({"agent_id": "thread-2", "hook_event_name": "SubagentStop"})
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(stop))
    mod.main()
    assert calls == []
    # A headless Claude session (no flag) still reports it.
    monkeypatch.delenv("OTO_HOOK_NO_FORWARD")
    monkeypatch.setattr(mod.sys, "stdin", io.StringIO(stop))
    mod.main()
    assert len(calls) == 1


def test_an_attended_chats_policy_follows_its_mode():
    """An attended app-server chat: Ask takes its sandbox's policy (the hook
    judges every call, the bridge takes a sandbox crossing), Auto runs
    on-request with Codex's own reviewer, Don't Ask runs under never; an
    unattended session keeps the floor (HOOKS.md "Codex chats")."""
    chat = CodexAppServerSession(
        session_id="11111111-2222-4333-8444-555555555557", agent_name="support",
        model="gpt-6", sandbox_mode="workspace-write", working_dir="",
        config_dir="/tmp/codex-home", permission_mode="ask",
    )
    assert chat.approval_policy == "on-request"
    chat.set_mode("plan", "read-only")
    assert chat.approval_policy == "on-request" and chat.plan_mode
    chat.set_mode("dontAsk", "danger-full-access")
    assert chat.approval_policy == "never" and not chat.plan_mode
    chat.set_mode("autoReview", "workspace-write")
    overrides = chat._thread_overrides()
    assert (overrides["approvalPolicy"], overrides["approvalsReviewer"]) == ("on-request", "auto_review")
    floored = _session(hooks_floor=True, sandbox_mode="workspace-write")
    assert floored.approval_policy == "on-request"
    assert _session(hooks_floor=True).approval_policy == "never"


def test_a_full_access_machines_ask_chat_is_judged_by_the_hook():
    """On a machine paired with full access Ask runs under
    danger-full-access: nothing crosses a sandbox, so the policy is never and
    the hook judges every call (its cards); Auto keeps the workspace sandbox
    and the reviewer, and a mode switch moves it both ways."""
    chat = _session(permission_mode="ask")  # sandbox danger-full-access
    assert chat.approval_policy == "never"
    chat.set_mode("autoReview", "workspace-write")
    assert chat.approval_policy == "on-request"
    chat.set_mode("dontAsk", "danger-full-access")
    assert chat.approval_policy == "never"
    chat.set_mode("ask", "danger-full-access")
    assert chat.approval_policy == "never"


@pytest.mark.asyncio
async def test_a_layer_mode_change_moves_the_policy_on_the_next_turn(monkeypatch):
    from core.layers.codex import layer as codex_layer
    chat = _session(permission_mode="ask", sandbox_mode="workspace-write")

    async def _get(_sid):
        return chat
    monkeypatch.setattr(codex_layer, "get_codex_session", _get)
    monkeypatch.setattr(codex_layer, "set_session_mode", lambda *_a: None)
    lay = codex_layer.CodexCLIExecutionLayer.__new__(codex_layer.CodexCLIExecutionLayer)
    await lay.change_mode(chat.session_id, "dontAsk")
    assert (chat.permission_mode, chat.sandbox_mode) == ("dontAsk", "danger-full-access")
    assert chat._thread_overrides()["approvalPolicy"] == "never"
    await lay.change_mode(chat.session_id, "plan")
    assert chat.plan_mode and chat.approval_policy == "on-request"
    await lay.change_mode(chat.session_id, "autoReview")
    assert (chat.permission_mode, chat.sandbox_mode) == ("autoReview", "workspace-write")
    assert chat._thread_overrides()["approvalsReviewer"] == "auto_review"


def test_layer_and_remote_payload_share_the_floor_rule():
    """The local layer and the remote start payload decide the floor with the
    SAME helper (helpers.codex_hooks_floor) — the satellite never re-derives it."""
    from core.layers.codex import helpers, layer
    assert layer.codex_hooks_floor is helpers.codex_hooks_floor
    for client_type in (k.name for k in session_kind.KINDS if not k.attended):
        assert helpers.codex_hooks_floor(client_type) is True
        assert helpers.codex_hooks_floor(client_type, interactive=True) is False
    assert helpers.codex_hooks_floor("dashboard") is False


class _Stop(Exception):
    pass


@pytest.mark.asyncio
async def test_a_resume_asks_for_no_turns(monkeypatch):
    """``thread/resume`` sends ``excludeTurns`` (Codex deprecates hydrating
    the history into the answer, which nothing here reads), never
    ``thread/start``."""
    calls = []

    class _Client:
        def set_server_request_handler(self, h):
            pass

        async def request(self, method, params=None):
            calls.append((method, dict(params or {})))
            raise _Stop()

    s = _session(thread_id="thread-1")

    async def _connect(env, cwd):
        s._client = _Client()
    monkeypatch.setattr(s, "_connect_with_retry", _connect)
    monkeypatch.setattr(s, "_build_env", lambda: {})
    with pytest.raises(_Stop):
        await s.start()
    resumes = [p for m, p in calls if m == "thread/resume"]
    assert resumes and all(p.get("excludeTurns") is True and p["threadId"] == "thread-1"
                           for p in resumes)
    assert len(resumes) == 1
    assert "excludeTurns" not in s._thread_overrides()


@pytest.mark.asyncio
@pytest.mark.parametrize("thread_id", ["thread-moved", None])
async def test_the_thread_runs_in_the_sandbox_folder(monkeypatch, thread_id):
    """``thread/resume`` and ``thread/start`` carry the sandbox's folder as
    ``cwd``: a resumed thread otherwise runs in the cwd its rollout recorded,
    and a thread carried from a machine recorded a folder the sandbox does not
    have (every stdio MCP failed at spawn there). The spawn cwd and the env's
    username stay ``working_dir``'s."""
    calls, spawn_cwds = [], []

    class _Client:
        def set_server_request_handler(self, h):
            pass

        async def request(self, method, params=None):
            calls.append((method, dict(params or {})))
            raise _Stop()

    s = _session(thread_id=thread_id, thread_cwd="/users/alice")

    async def _connect(env, cwd):
        spawn_cwds.append(cwd)
        s._client = _Client()
    monkeypatch.setattr(s, "_connect_with_retry", _connect)
    monkeypatch.setattr(s, "_build_env", lambda: {})
    with pytest.raises(_Stop):
        await s.start()
    method, params = calls[0]
    assert method == ("thread/resume" if thread_id else "thread/start")
    assert params["cwd"] == "/users/alice"
    assert spawn_cwds == [None]
    assert "cwd" not in _session()._thread_overrides()


@pytest.mark.asyncio
async def test_a_refused_resume_starts_a_new_thread_and_says_so(monkeypatch):
    """A thread Codex will not open (a moved chat whose rollout it cannot
    read): a new thread starts, and the session says the resume was refused
    so the layer reseeds the chat from its history."""
    from core.layers.codex.app_server_client import AppServerError

    class _Client:
        def set_server_request_handler(self, h):
            pass

        async def request(self, method, params=None):
            if method == "thread/resume":
                raise AppServerError("no rollout found for thread id thread-moved")
            return {"thread": {"id": "thread-fresh"}}

    s = _session(thread_id="thread-moved")

    async def _connect(env, cwd):
        s._client = _Client()

    async def _warm():
        raise _Stop()
    monkeypatch.setattr(s, "_connect_with_retry", _connect)
    monkeypatch.setattr(s, "_build_env", lambda: {})
    monkeypatch.setattr(s, "_warm_mcps", _warm)
    with pytest.raises(_Stop):
        await s.start()
    assert s.thread_id == "thread-fresh" and s.resume_refused is True
