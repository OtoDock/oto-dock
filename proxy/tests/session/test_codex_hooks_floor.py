"""The Codex hook floor (docs/execution-layers/CODEX.md, "Hook floor"), the
platform's half: which session kinds nobody answers, the hook environment
a daemon and a terminal start with, and the PostToolUse forwarder and the
sub-agent tracker staying quiet under it. The
thread-level hook trust and the approval policy by mode are the machine's
(``satellite/tests/test_codex_session.py``, ``TestHooksFloor``)."""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib

import config as app_config
from core.layers.codex.env import daemon_env
from core.session import session_kind

_SID = "11111111-2222-4333-8444-555555555555"


def _daemon_env(extra_env: dict | None = None) -> dict:
    return daemon_env(_SID, "support", username="", user_role="",
                      codex_bin="/usr/bin/codex", extra_env=extra_env or {})


def test_the_unattended_kinds_are_the_no_human_set():
    unattended = {k.name for k in session_kind.KINDS if not k.attended}
    assert unattended == {"task", "phone", "meeting", "trigger", "internal", "app"}
    assert session_kind.attended("dashboard") and session_kind.attended("")


def test_daemon_env_marks_deny_only_and_no_forward(monkeypatch):
    """The hook judges every call of every session: no spawn sets
    ``OTO_HOOK_BRIDGED``, and a stray value from the session's own env is
    popped before it reaches one."""
    from core.sandbox import env_builder
    monkeypatch.setattr(env_builder, "build_session_env", lambda *a, **k: {"PATH": "/usr/bin"})
    env = _daemon_env()
    assert env["OTO_HOOK_DENY_ONLY"] == "1" and env["OTO_HOOK_NO_FORWARD"] == "1"
    assert "OTO_HOOK_BRIDGED" not in env
    assert "OTO_HOOK_BRIDGED" not in _daemon_env({"OTO_HOOK_BRIDGED": "1"})


def test_the_daemon_env_turns_the_pagers_escapes_off(monkeypatch):
    """``less`` starts with no card and Codex calls no hook for text typed
    into a running terminal, so the pager's own ``!command`` would run a
    command nobody judged. The session's own env cannot turn it back on."""
    from core.sandbox import env_builder
    monkeypatch.setattr(env_builder, "build_session_env", lambda *a, **k: {"PATH": "/usr/bin"})
    assert _daemon_env()["LESSSECURE"] == "1"
    assert _daemon_env({"LESSSECURE": "0"})["LESSSECURE"] == "1"


def test_the_daemon_env_carries_a_command_line_token_over_the_final_session_token(monkeypatch):
    """Codex's shell inherits the daemon's env: the token mirrors the
    session token the daemon ends up with (the session's own env, applied
    after the builder's, may replace it)."""
    from auth.session_token import create_session_token, validate_cli_token
    from core.sandbox import env_builder
    monkeypatch.setattr(env_builder, "build_session_env", lambda *a, **k: {
        "PATH": "/usr/bin", "PROXY_API_KEY": create_session_token(_SID, "support", "")})
    final = create_session_token(_SID, "support", "user-x")
    env = _daemon_env({"PROXY_API_KEY": final})
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


def test_the_spawn_plan_and_the_remote_payload_share_the_floor_rule():
    """The spawn plan and the remote start payload decide the floor with the
    SAME helper (helpers.codex_hooks_floor) — the satellite never re-derives it."""
    from core.layers.codex import helpers, remote, spawn
    assert spawn.codex_hooks_floor is helpers.codex_hooks_floor
    assert remote.codex_hooks_floor is helpers.codex_hooks_floor
    for client_type in (k.name for k in session_kind.KINDS if not k.attended):
        assert helpers.codex_hooks_floor(client_type) is True
        assert helpers.codex_hooks_floor(client_type, interactive=True) is False
    assert helpers.codex_hooks_floor("dashboard") is False
