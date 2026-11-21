#!/usr/bin/env python3
"""Stop hook: tells the proxy the turn ended, and asks whether it may end.

Runs on both engines (Claude Code ``settings.json`` and Codex ``hooks.json``
name it). It asks the platform's ``turn_end`` verdict, decided proxy-side
(``api/hooks/lifecycle.py::hook_stop``, ``docs/architecture/HOOKS.md``).
``transcript_path`` rides along as text; the proxy finds a terminal's
transcript itself and never opens the posted path.

* The platform's ``turn_end`` verdict. Where the CLI drives the turn (the
  terminals) this hook is the one point that can hold it open: when the
  proxy answers ``{"decision": "block", "reason": …}`` the script prints
  that shape — the one BOTH engines accept on Stop (Codex rejects
  ``hookSpecificOutput`` here) — and the agent continues with the reason as
  a new message. Where the proxy drives the turn (headless sessions) it
  answers nothing and the layer's loop delivers any verdict.

Fails OPEN: a missing or slow proxy lets the agent stop (nothing runs that
was not going to; the platform reports a check it could not run). The
permission gate is the one that fails closed. A terminal's Stop that never
reached the platform (its machine's link was down: the tunnel's own 503, or
a refused connection) is sent again for up to 30 s first, so the turn's
checks run once the link is back. Same shape as the other scripts: JSON on
stdin, stdlib-only, exit 0 always.

Environment variables (set by the proxy / satellite via subprocess env, inherited
by hook subprocesses):
  PROXY_URL         - e.g. http://127.0.0.1:8400
  PROXY_API_KEY     - Bearer token for auth
  OTO_SESSION_ID    - session UUID for this conversation
  OTO_INTERACTIVE   - set on a terminal session (the CLI drives the turn)
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

# The proxy bounds a verdict's wait itself (a check's own timeout); this is
# the transport ceiling, the same week the permission gate uses. Only a
# terminal waits for a verdict: a proxy-driven session's Stop is an
# observation the proxy answers at once, so a stalled connection there must
# not hold the turn end.
_VERDICT_TIMEOUT_S = 604800
_OBSERVATION_TIMEOUT_S = 10
# A terminal's Stop that never reached the platform goes again after each
# pause, 30 s in all: a machine's link is back a second or two after a drop
# or a wake. Shorter than the permission gate's hold, since this hook fails
# open and holds the turn's end while it waits.
_RESEND_PAUSES_S = (1.0, 2.0, 4.0, 8.0, 15.0)
# The agent's final message travels for a handler that judges the answer;
# capped so a long reply never becomes a large hook payload.
_LAST_MESSAGE_MAX = 8192
# Claude's background work travels as its kind labels only, capped.
_KIND_MAX = 40
_KINDS_MAX = 16


def _background_kinds(inp):
    """The kinds of Claude's ``background_tasks`` (the work still in flight
    when the turn ends: ``shell``, ``subagent``, ...), sorted and distinct, or
    None when the input carries no such list (Codex, an older CLI). Only the
    labels travel: a task's command and description hold whatever the agent
    typed. Never raises: this payload is a terminal's one turn-end signal."""
    tasks = inp.get("background_tasks")
    if not isinstance(tasks, list):
        return None
    kinds = set()
    for task in tasks:
        kind = task.get("type") if isinstance(task, dict) else None
        if isinstance(kind, str) and kind:
            kinds.add(kind[:_KIND_MAX])
    return sorted(kinds)[:_KINDS_MAX]


def _never_reached(exc):
    """The request never reached the platform: the machine's tunnel refused
    it because its link was down (503 ``tunnel-not-connected``), or nothing
    took the connection. A 502, a 504, a reset or a timeout may have reached
    it, and a second Stop would judge the turn twice."""
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code != 503:
            return False
        try:
            body = json.loads(exc.read() or b"{}")
        except (OSError, http.client.HTTPException, ValueError, AttributeError):
            return False
        return isinstance(body, dict) and body.get("error") == "tunnel-not-connected"
    if isinstance(exc, urllib.error.URLError):
        exc = exc.reason
    return isinstance(exc, ConnectionRefusedError)


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
    fields = {
        "session_id": session_id,
        # Both CLIs provide the transcript path natively in the Stop input
        # (Claude: the session JSONL; Codex: the rollout).
        "transcript_path": inp.get("transcript_path") or "",
        "hook_event_name": inp.get("hook_event_name", "Stop"),
        "stop_hook_active": bool(inp.get("stop_hook_active", False)),
        "last_assistant_message": last[:_LAST_MESSAGE_MAX],
    }
    kinds = _background_kinds(inp)
    if kinds is not None:
        fields["background_kinds"] = kinds
    payload = json.dumps(fields).encode()

    req = urllib.request.Request(
        f"{proxy_url}/v1/hooks/stop",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    terminal = bool(os.environ.get("OTO_INTERACTIVE"))
    timeout = _VERDICT_TIMEOUT_S if terminal else _OBSERVATION_TIMEOUT_S
    pauses = list(_RESEND_PAUSES_S) if terminal else []
    while True:
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read() or b"{}")
            break
        except (OSError, http.client.HTTPException, ValueError) as exc:
            if not pauses or not _never_reached(exc):
                return  # fail open — never hold the agent on a missing proxy
            time.sleep(pauses.pop(0))

    if isinstance(result, dict) and result.get("decision") == "block":
        reason = result.get("reason") or ""
        if isinstance(reason, str) and reason.strip():
            print(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    # PROXY_URL is a loopback address (the platform's own listener, or a
    # satellite's tunnel): no proxy may stand between. urllib's default
    # opener takes the environment's proxy and, on Windows, Internet
    # Options' (whose <local> exemption skips only dotless hosts, never
    # 127.0.0.1). Installed here, never at import: a test that loads the
    # script keeps its own opener.
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    try:
        main()
    except Exception:  # noqa: BLE001  # the stop hook exits 0 whatever failed, the agent must be free to stop
        sys.exit(0)
