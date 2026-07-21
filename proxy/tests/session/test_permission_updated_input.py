"""decide_tool_permission → updated_input passthrough.

Pass-1 (check_tool_access) rewrites sandbox-virtual / ``~`` native-tool path
args to their satellite-host form on remote sessions. The wrapper attaches
that rewrite to whatever ALLOW the mode branching ultimately returns, and the
Claude PreToolUse hook emits it as ``updatedInput``. These tests pin the
endpoint contract; the rewrite matrix itself is covered by
``tests/remote/test_path_policy_remote_integration.py``.
"""
import json
import subprocess
import sys

import pytest

from api.hooks import hooks
from auth.path_policy import SecurityContext
from tests._paths import PROXY_DIR

_GATE_SCRIPT = PROXY_DIR / "hooks" / "permission_gate.py"


def _remote_ctx(role: str = "manager") -> SecurityContext:
    return SecurityContext(
        role=role,
        username="dave",
        agent="my-agent",
        is_admin_agent=False,
        target_kind="user_remote",
        target_label="dev-box",
        target_agents_dir="/home/dave/.oto-dock/agents",
        target_machine_id="machine-abc",
        target_home_dir="/home/dave",
        target_allow_full_fs=False,
    )


@pytest.fixture
def _stub(monkeypatch):
    """Session lookups stubbed so decide_tool_permission runs DB-free with a
    remote security context in auto mode (no prompt round-trip)."""
    monkeypatch.setattr(hooks, "record_hook_activity", lambda sid: None)
    monkeypatch.setattr(hooks, "get_meeting_session_info", lambda sid: None)
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "auto")
    monkeypatch.setattr(hooks, "get_session_client_type", lambda sid: "task")
    monkeypatch.setattr(hooks, "get_session_security", lambda sid: _remote_ctx())
    # Target-revocation check hits the DB — the target is valid here.
    from services import path_policy_v2
    monkeypatch.setattr(path_policy_v2, "check_target_still_valid", lambda ctx: "")


@pytest.mark.asyncio
async def test_allow_carries_updated_input(_stub):
    res = await hooks.decide_tool_permission(
        "s", "Read", {"file_path": "/workspace/notes.md"},
    )
    assert res["decision"] == "allow"
    assert res["updated_input"] == {
        "file_path": "/home/dave/.oto-dock/agents/my-agent/workspace/notes.md",
    }


@pytest.mark.asyncio
async def test_native_path_allow_has_no_updated_input(_stub):
    res = await hooks.decide_tool_permission(
        "s", "Read", {"file_path": "/home/dave/Desktop/foo.png"},
    )
    assert res["decision"] == "allow"
    assert "updated_input" not in res


@pytest.mark.asyncio
async def test_ask_carries_updated_input(_stub, monkeypatch):
    # Interactive TUI + default mode: a write-tier tool returns "ask" (the
    # CLI renders its native prompt) — the rewrite must still ride along, or
    # the tool runs against the raw sandbox-virtual path and fails ENOENT.
    monkeypatch.setattr(hooks, "get_session_mode", lambda sid: "default")
    monkeypatch.setattr(hooks, "get_session_client_type", lambda sid: "dashboard")
    monkeypatch.setattr(hooks, "_is_interactive_session", lambda sid: True)
    res = await hooks.decide_tool_permission(
        "s", "Write", {"file_path": "/workspace/notes.md", "content": "x"},
    )
    assert res["decision"] == "ask"
    assert res["updated_input"]["file_path"] == (
        "/home/dave/.oto-dock/agents/my-agent/workspace/notes.md"
    )


@pytest.mark.asyncio
async def test_deny_never_carries_updated_input(_stub, monkeypatch):
    monkeypatch.setattr(
        hooks, "get_session_security", lambda sid: _remote_ctx(role="viewer"),
    )
    res = await hooks.decide_tool_permission(
        "s", "Write", {"file_path": "/knowledge/x.md", "content": "y"},
    )
    assert res["decision"] == "deny"
    assert "updated_input" not in res


def _run_gate(hook_stdin: dict, proxy_response: dict,
              extra_env: dict | None = None) -> dict | None:
    """Run proxy/hooks/permission_gate.py as the CLI would, with
    urllib.request.urlopen stubbed to return ``proxy_response``. Returns the
    parsed hookSpecificOutput JSON, or None when the gate emitted nothing."""
    stub = (
        "import json, sys, urllib.request\n"
        "class _Resp:\n"
        "    def __init__(self, body): self._body = body\n"
        "    def read(self): return self._body\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): return False\n"
        f"_BODY = json.dumps({proxy_response!r}).encode()\n"
        "urllib.request.urlopen = lambda req, timeout=0: _Resp(_BODY)\n"
        "sys.argv = ['permission_gate.py']\n"
        f"exec(compile(open({str(_GATE_SCRIPT)!r}).read(), 'permission_gate.py', 'exec'))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", stub],
        input=json.dumps(hook_stdin),
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "OTO_SESSION_ID": "s",
            "PROXY_URL": "http://127.0.0.1:1",
            "PROXY_API_KEY": "k",
            "PATH": "/usr/bin:/bin",
            **(extra_env or {}),
        },
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def test_gate_emits_updated_input_on_allow():
    out = _run_gate(
        {"tool_name": "Read", "tool_input": {"file_path": "/workspace/x"}},
        {"decision": "allow", "updated_input": {"file_path": "/real/x"}},
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert hso["updatedInput"] == {"file_path": "/real/x"}
    # The rewrite also teaches the model the native mapping (Edit/NotebookEdit
    # can't be rewritten — their validation runs before hooks).
    assert "/workspace/x" in hso["additionalContext"]
    assert "/real/x" in hso["additionalContext"]


def test_gate_ask_emits_decision_with_rewrite():
    # Interactive residual-ask: an explicit permissionDecision:"ask" forces
    # the CLI's native prompt (hook silence would be treated as allow in a
    # trusted workspace dir); the corrected input + path note ride along.
    out = _run_gate(
        {"tool_name": "Write", "tool_input": {"file_path": "/workspace/x"}},
        {"decision": "ask", "reason": "needs approval",
         "updated_input": {"file_path": "/real/x"}},
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask"
    assert hso["permissionDecisionReason"] == "needs approval"
    assert hso["updatedInput"] == {"file_path": "/real/x"}
    assert "/real/x" in hso["additionalContext"]


def test_gate_ask_plain_emits_ask():
    out = _run_gate(
        {"tool_name": "Write", "tool_input": {"file_path": "/real/x"}},
        {"decision": "ask"},
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "ask"
    assert "updatedInput" not in hso


def test_gate_deny_only_env_suppresses_ask():
    # Codex spawns set OTO_HOOK_DENY_ONLY=1 — any non-deny stays silent,
    # including an ask that carries a rewrite (Codex rejects non-deny JSON;
    # its own -a on-request layer prompts instead).
    out = _run_gate(
        {"tool_name": "Write", "tool_input": {"file_path": "/workspace/x"}},
        {"decision": "ask", "updated_input": {"file_path": "/real/x"}},
        extra_env={"OTO_HOOK_DENY_ONLY": "1"},
    )
    assert out is None


def test_gate_forwards_permission_mode():
    # The CLI reports its LIVE mode (Shift+Tab) on every hook call; the gate
    # must forward it to the proxy, which uses it as the effective mode for
    # interactive sessions.
    stub = (
        "import json, sys, urllib.request\n"
        "class _Resp:\n"
        "    def __init__(self, body): self._body = body\n"
        "    def read(self): return self._body\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): return False\n"
        "def _urlopen(req, timeout=0):\n"
        "    sent = json.loads(req.data)\n"
        "    return _Resp(json.dumps({'decision': 'deny',\n"
        "        'reason': 'mode=' + sent.get('permission_mode', '<missing>')}).encode())\n"
        "urllib.request.urlopen = _urlopen\n"
        "sys.argv = ['permission_gate.py']\n"
        f"exec(compile(open({str(_GATE_SCRIPT)!r}).read(), 'permission_gate.py', 'exec'))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", stub],
        input=json.dumps({"tool_name": "Write", "tool_input": {},
                          "permission_mode": "acceptEdits"}),
        capture_output=True, text=True, timeout=30,
        env={"OTO_SESSION_ID": "s", "PROXY_URL": "http://127.0.0.1:1",
             "PROXY_API_KEY": "k", "PATH": "/usr/bin:/bin"},
    )
    assert proc.returncode == 0, proc.stderr
    hso = json.loads(proc.stdout.strip())["hookSpecificOutput"]
    assert hso["permissionDecisionReason"] == "mode=acceptEdits"


def test_gate_omits_updated_input_on_deny():
    out = _run_gate(
        {"tool_name": "Write", "tool_input": {"file_path": "/knowledge/x"}},
        {"decision": "deny", "reason": "nope",
         "updated_input": {"file_path": "/real/x"}},
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "updatedInput" not in hso


def test_gate_plain_allow_unchanged():
    out = _run_gate(
        {"tool_name": "Read", "tool_input": {"file_path": "/real/x"}},
        {"decision": "allow"},
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
    assert "updatedInput" not in hso


def _run_gate_transport(hook_stdin: dict, fail_times: int,
                        proxy_response: dict | None = None) -> dict | None:
    """Run the gate with urlopen raising ``fail_times`` times before (optionally)
    succeeding with ``proxy_response``. time.sleep is stubbed out so the retry
    ladder doesn't slow the suite."""
    stub = (
        "import json, sys, time, urllib.request\n"
        "time.sleep = lambda s: None\n"
        "class _Resp:\n"
        "    def __init__(self, body): self._body = body\n"
        "    def read(self): return self._body\n"
        "    def __enter__(self): return self\n"
        "    def __exit__(self, *a): return False\n"
        f"_BODY = json.dumps({proxy_response!r}).encode()\n"
        f"_fails = [{fail_times}]\n"
        "def _urlopen(req, timeout=0):\n"
        "    if _fails[0] > 0:\n"
        "        _fails[0] -= 1\n"
        "        raise OSError('connection refused')\n"
        "    return _Resp(_BODY)\n"
        "urllib.request.urlopen = _urlopen\n"
        "sys.argv = ['permission_gate.py']\n"
        f"exec(compile(open({str(_GATE_SCRIPT)!r}).read(), 'permission_gate.py', 'exec'))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", stub],
        input=json.dumps(hook_stdin),
        capture_output=True,
        text=True,
        timeout=30,
        env={
            "OTO_SESSION_ID": "s",
            "PROXY_URL": "http://127.0.0.1:1",
            "PROXY_API_KEY": "k",
            "PATH": "/usr/bin:/bin",
        },
    )
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def test_gate_fails_closed_when_platform_unreachable():
    # Every attempt (initial + retries) fails → explicit DENY, never a
    # silent allow (a proxy restart must not auto-approve a pending gate).
    out = _run_gate_transport(
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}},
        fail_times=99,
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "unreachable" in hso["permissionDecisionReason"]


def test_gate_retries_transient_failure_then_honors_decision():
    # Two failures ride the retry ladder; the third attempt reaches the proxy.
    out = _run_gate_transport(
        {"tool_name": "Read", "tool_input": {"file_path": "/real/x"}},
        fail_times=2,
        proxy_response={"decision": "allow"},
    )
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "allow"
