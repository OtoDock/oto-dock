#!/usr/bin/env python3
"""SubagentStop hook: tells the proxy a subagent finished.

This is the deterministic, idle-safe completion signal for Claude Code
subagents (foreground AND background). It fires out-of-band over HTTP the
moment a subagent stops — unlike the stdout `task_notification`, which stalls
while the `-p` process is idle (exactly when background agents finish). The
proxy correlates `agent_id` (== the CLI task_id) back to the spawning
tool_use_id via the per-session SubagentRegistry.

Same shape as permission_gate.py / tool_result_forwarder.py: reads JSON from
stdin, uses only urllib.request (no dependencies), POSTs to the proxy, exits
fast. Never blocks or fails the agent.

Environment variables (set by the proxy / satellite via subprocess env, and
inherited by subagent-lifecycle hook subprocesses):
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

    # Interactive TUI (OTO_INTERACTIVE): redundant — the transcript tailer feeds
    # the SubagentRegistry (register_spawn from Task tool_use, mark_done from the
    # tool_result), so this no-ops. Headless -p still uses it.
    if os.environ.get("OTO_INTERACTIVE"):
        return

    session_id = os.environ.get("OTO_SESSION_ID", "")
    proxy_url = os.environ.get("PROXY_URL", "")
    api_key = os.environ.get("PROXY_API_KEY", "")

    if not proxy_url or not api_key or not session_id:
        return

    # The CLI's agent_id for a SubagentStop equals the task_started.task_id.
    agent_id = inp.get("agent_id", "")
    if not agent_id:
        return

    payload = json.dumps({
        "session_id": session_id,
        "agent_id": agent_id,
        "agent_type": inp.get("agent_type", ""),
        "hook_event_name": inp.get("hook_event_name", "SubagentStop"),
    }).encode()

    req = urllib.request.Request(
        f"{proxy_url}/v1/hooks/subagent",
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
