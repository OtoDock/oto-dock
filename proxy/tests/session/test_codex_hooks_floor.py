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
from core.session import session_kind
from core.layers.codex import session as codex_session
from core.layers.codex.session import CodexAppServerSession


def _session(**kw) -> CodexAppServerSession:
    return CodexAppServerSession(
        session_id="11111111-2222-4333-8444-555555555555", agent_name="support",
        model="gpt-6", sandbox_mode="danger-full-access", working_dir="",
        config_dir="/tmp/codex-home", **kw,
    )


def test_the_unattended_kinds_are_the_no_human_set():
    unattended = {k.name for k in session_kind.KINDS if not k.attended}
    assert unattended == {"task", "phone", "meeting", "trigger", "internal", "app"}
    assert session_kind.attended("dashboard") and session_kind.attended("")


def test_thread_overrides_carry_hook_trust_only_for_the_floor():
    plain = _session()._thread_overrides()
    assert "config" not in plain and plain["approvalPolicy"] == "never"
    floored = _session(hooks_floor=True)._thread_overrides()
    assert floored["config"] == {"bypass_hook_trust": True}
    assert floored["approvalPolicy"] == "never" and floored["approvalsReviewer"] == "user"


def test_daemon_env_marks_deny_only_and_no_forward(monkeypatch):
    from core.sandbox import env_builder
    monkeypatch.setattr(env_builder, "build_session_env", lambda *a, **k: {"PATH": "/usr/bin"})
    monkeypatch.setattr(app_config, "CODEX_BIN", "/usr/bin/codex", raising=False)
    env = _session(hooks_floor=True)._build_env()
    assert env["OTO_HOOK_DENY_ONLY"] == "1" and env["OTO_HOOK_NO_FORWARD"] == "1"
    env = _session()._build_env()
    assert "OTO_HOOK_DENY_ONLY" not in env and "OTO_HOOK_NO_FORWARD" not in env


@pytest.mark.asyncio
async def test_create_codex_session_threads_the_flag(monkeypatch):
    seen: dict = {}

    async def _start(self):
        seen["hooks_floor"] = self.hooks_floor
    monkeypatch.setattr(CodexAppServerSession, "start", _start)
    s = await codex_session.create_codex_session(
        "11111111-2222-4333-8444-555555555556", "support", "gpt-6",
        config_dir="/tmp/codex-home", hooks_floor=True,
    )
    try:
        assert seen == {"hooks_floor": True} and s.hooks_floor is True
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


def test_attended_chats_send_untrusted_and_fall_back_once_on_rejection():
    """An attended app-server chat (no floor) asks for every command and
    patch through the bridge (approvalPolicy "untrusted"); a daemon that
    rejects the value drops the session to on-request once; unattended
    sessions keep never/on-request as before (HOOKS.md "Attended Codex
    chats")."""
    from core.layers.codex.app_server_client import AppServerError
    attended = CodexAppServerSession(
        session_id="11111111-2222-4333-8444-555555555557", agent_name="support",
        model="gpt-6", sandbox_mode="workspace-write", working_dir="",
        config_dir="/tmp/codex-home",
    )
    assert attended.approval_policy == "untrusted"
    assert attended._thread_overrides()["approvalPolicy"] == "untrusted"
    attended.set_sandbox_mode("read-only")
    assert attended.approval_policy == "untrusted"
    attended.set_sandbox_mode("danger-full-access")
    assert attended.approval_policy == "never"
    attended.set_sandbox_mode("workspace-write")
    # The rejection: once, then on-request for the session.
    assert attended._note_untrusted_rejection(AppServerError("unknown variant `untrusted`")) is True
    assert attended.approval_policy == "on-request"
    assert attended._note_untrusted_rejection(AppServerError("unknown variant `untrusted`")) is False
    # Any other error is not the fallback trigger.
    fresh = CodexAppServerSession(
        session_id="11111111-2222-4333-8444-555555555558", agent_name="support",
        model="gpt-6", sandbox_mode="workspace-write", working_dir="",
        config_dir="/tmp/codex-home",
    )
    assert fresh._note_untrusted_rejection(AppServerError("thread not found")) is False
    assert fresh.approval_policy == "untrusted"
    # Unattended: the floor, never untrusted.
    floored = _session(hooks_floor=True, sandbox_mode="workspace-write") if False else CodexAppServerSession(
        session_id="11111111-2222-4333-8444-555555555559", agent_name="support",
        model="gpt-6", sandbox_mode="workspace-write", working_dir="",
        config_dir="/tmp/codex-home", hooks_floor=True,
    )
    assert floored.approval_policy == "on-request"
    assert _session(hooks_floor=True).approval_policy == "never"


def test_layer_and_remote_payload_share_the_floor_rule():
    """The local layer and the remote start payload decide the floor with the
    SAME helper (helpers.codex_hooks_floor) — the satellite never re-derives it."""
    from core.layers.codex import helpers, layer
    assert layer.codex_hooks_floor is helpers.codex_hooks_floor
    for client_type in (k.name for k in session_kind.KINDS if not k.attended):
        assert helpers.codex_hooks_floor(client_type) is True
        assert helpers.codex_hooks_floor(client_type, interactive=True) is False
    assert helpers.codex_hooks_floor("dashboard") is False
