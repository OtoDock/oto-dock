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

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Within the hook's 10 s: a post refused while a machine's link to the
# platform reconnects (the tunnel answers 503) or a refused connection is
# sent again a little later; a longer outage is healed by the CLI's own
# completion line, which the platform reads from the session's output.
_ATTEMPTS = 3
_PAUSES_S = (1.0, 2.0)
_POST_TIMEOUT_S = 2.0


def main():
    raw = sys.stdin.read()
    # Codex app-server sessions (OTO_HOOK_NO_FORWARD): the thread router
    # registers and resolves the sub-agents from the JSON-RPC stream. A
    # SubagentStop fires at every child turn end, so a post here would park
    # a reused child as finished before it runs again.
    if os.environ.get("OTO_HOOK_NO_FORWARD"):
        return
    try:
        inp = json.loads(raw)
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

    for attempt in range(_ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_S):
                return
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return  # refused for good (a stale token): never interrupt Claude
        except (OSError, http.client.HTTPException, ValueError):
            pass
        if attempt < len(_PAUSES_S):
            time.sleep(_PAUSES_S[attempt])


if __name__ == "__main__":
    # PROXY_URL is a loopback address (the platform's own listener, or a
    # satellite's tunnel): no proxy may stand between. urllib's default
    # opener takes the environment's proxy and, on Windows, Internet
    # Options' (whose <local> exemption skips only dotless hosts, never
    # 127.0.0.1). Installed here, never at import: a test that loads the
    # script keeps its own opener.
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    main()
