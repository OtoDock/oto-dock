#!/usr/bin/env python3
"""Stop hook: hands the proxy the transcript path at turn end.

Interactive CLI sessions run the native TUI under a PTY and do NOT flow through
the pump, so the proxy has no other turn-end signal or
transcript pointer for them. This hook fires when the main agent finishes a turn;
the proxy reads the Claude JSONL at ``transcript_path`` and appends new messages
to ``chat_messages``. The receiver no-ops for headless ``-p`` sessions
(which the pump already persists), so wiring this in the shared settings.json is
harmless for them.

Same shape as subagent_tracker.py / permission_gate.py: reads JSON from stdin,
stdlib-only (urllib), POSTs to the proxy, exits fast. Never blocks or fails the
agent.

Environment variables (set by the proxy / satellite via subprocess env, inherited
by hook subprocesses):
  PROXY_URL         - e.g. http://127.0.0.1:8400
  PROXY_API_KEY     - Bearer token for auth
  OTO_SESSION_ID    - session UUID for this conversation
"""

import json
import os
import sys
import urllib.request


def main():
    try:
        inp = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return

    session_id = os.environ.get("OTO_SESSION_ID", "")
    proxy_url = os.environ.get("PROXY_URL", "")
    api_key = os.environ.get("PROXY_API_KEY", "")

    if not proxy_url or not api_key or not session_id:
        return

    payload = json.dumps({
        "session_id": session_id,
        # Claude provides the transcript path natively in the Stop hook input.
        "transcript_path": inp.get("transcript_path", ""),
        "hook_event_name": inp.get("hook_event_name", "Stop"),
    }).encode()

    req = urllib.request.Request(
        f"{proxy_url}/v1/hooks/stop",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10):
            pass  # Fire and forget
    except Exception:
        pass  # Non-blocking — never interrupt Claude


if __name__ == "__main__":
    main()
