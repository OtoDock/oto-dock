#!/usr/bin/env python3
"""Stop hook: tells the proxy the turn ended, and asks whether it may end.

Runs on both engines (Claude Code ``settings.json`` and Codex ``hooks.json``
name it). Two jobs, decided proxy-side (``api/hooks/lifecycle.py::hook_stop``,
``docs/architecture/HOOKS.md``):

* A Claude TERMINAL session (the native TUI under a PTY, no pump) has no
  other turn-end signal or transcript pointer, so the proxy reads the JSONL
  at ``transcript_path`` and appends the new messages to chat history.
* The platform's ``turn_end`` verdict. Where the CLI drives the turn (the
  terminals) this hook is the one point that can hold it open: when the
  proxy answers ``{"decision": "block", "reason": …}`` the script prints
  that shape — the one BOTH engines accept on Stop (Codex rejects
  ``hookSpecificOutput`` here) — and the agent continues with the reason as
  a new message. Where the proxy drives the turn (headless sessions) it
  answers nothing and the layer's loop delivers any verdict.

Fails OPEN: a missing or slow proxy lets the agent stop (nothing runs that
was not going to; the platform reports a check it could not run). The
permission gate is the one that fails closed. Same shape as the other
scripts: JSON on stdin, stdlib-only, one POST, exit 0 always.

Environment variables (set by the proxy / satellite via subprocess env, inherited
by hook subprocesses):
  PROXY_URL         - e.g. http://127.0.0.1:8400
  PROXY_API_KEY     - Bearer token for auth
  OTO_SESSION_ID    - session UUID for this conversation
  OTO_INTERACTIVE   - set on a terminal session (the CLI drives the turn)
"""

import json
import os
import sys
import urllib.request

# The proxy bounds a verdict's wait itself (a check's own timeout); this is
# the transport ceiling, the same week the permission gate uses. Only a
# terminal waits for a verdict: a proxy-driven session's Stop is an
# observation the proxy answers at once, so a stalled connection there must
# not hold the turn end.
_VERDICT_TIMEOUT_S = 604800
_OBSERVATION_TIMEOUT_S = 10
# The agent's final message travels for a handler that judges the answer;
# capped so a long reply never becomes a large hook payload.
_LAST_MESSAGE_MAX = 8192


def main():
    try:
        inp = json.loads(sys.stdin.read())
    except (OSError, json.JSONDecodeError, ValueError):
        return

    session_id = os.environ.get("OTO_SESSION_ID", "")
    proxy_url = os.environ.get("PROXY_URL", "")
    api_key = os.environ.get("PROXY_API_KEY", "")

    if not proxy_url or not api_key or not session_id:
        return

    last = inp.get("last_assistant_message") or ""
    if not isinstance(last, str):
        last = ""
    payload = json.dumps({
        "session_id": session_id,
        # Both CLIs provide the transcript path natively in the Stop input
        # (Claude: the session JSONL; Codex: the rollout).
        "transcript_path": inp.get("transcript_path") or "",
        "hook_event_name": inp.get("hook_event_name", "Stop"),
        "stop_hook_active": bool(inp.get("stop_hook_active", False)),
        "last_assistant_message": last[:_LAST_MESSAGE_MAX],
    }).encode()

    req = urllib.request.Request(
        f"{proxy_url}/v1/hooks/stop",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    timeout = _VERDICT_TIMEOUT_S if os.environ.get("OTO_INTERACTIVE") else _OBSERVATION_TIMEOUT_S
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read() or b"{}")
    except Exception:
        return  # fail open — never hold the agent on a missing proxy

    if isinstance(result, dict) and result.get("decision") == "block":
        reason = result.get("reason") or ""
        if isinstance(reason, str) and reason.strip():
            print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
