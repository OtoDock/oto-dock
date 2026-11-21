#!/usr/bin/env python3
"""An engine's SessionStart hook: the start's riding part.

A session's text carries only what changes rarely, so a start inside the
engine's cache lifetime reads the conversation from the cache. The data
that changes (memory, notes, Notebook entries, the folder tree, the
parallel sessions) is asked from the proxy here and printed as the start's
added context, after the history, which the engine's screen does not draw.
Claude Code inlines a hook's context only up to 10,000 characters, so its
terminal runs several entries of this script, ``start_context.py <part>
<parts>``, and each prints its part (the proxy renders the parts once per
start). Codex takes a context of any size: one entry, part 1 of 1, in a
terminal and in a headless session alike.

A headless Claude Code session gets its start as its own message from the
proxy, so there (neither ``OTO_INTERACTIVE`` nor ``CODEX_HOME``) this script
answers only a compaction inside a turn (the ``compact`` source), which the
CLI runs before the turn goes on; any other start ends before an import (an
event that names "compact" elsewhere reads its source first and ends before
the network is imported): a Claude start runs ten of these. It prints nothing and exits 0 on anything
unexpected: the start goes on without the part, and the data stays one tool
call away.

Environment: ``PROXY_URL``, ``PROXY_API_KEY``, ``OTO_SESSION_ID``,
``OTO_INTERACTIVE``, ``CODEX_HOME`` (set on a Codex engine).
"""

import os
import sys

_TIMEOUT_S = 15


def main():
    codex = bool(os.environ.get("CODEX_HOME"))
    headless_claude = not (os.environ.get("OTO_INTERACTIVE") or codex)
    raw = sys.stdin.read()
    if headless_claude and '"compact"' not in raw:
        return
    proxy_url = os.environ.get("PROXY_URL", "")
    session_id = os.environ.get("OTO_SESSION_ID", "")
    if not (proxy_url and session_id):
        return
    import json
    try:
        part = int(sys.argv[1]) if len(sys.argv) > 1 else 1
        parts = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        event = json.loads(raw or "{}")
    except ValueError:
        return
    if headless_claude and event.get("source") != "compact":
        return
    import urllib.request
    body = json.dumps({
        "session_id": session_id,
        "conversation": str(event.get("session_id") or ""),
        "source": str(event.get("source") or ""),
        "part": part,
        "parts": parts,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{proxy_url.rstrip('/')}/v1/hooks/start-context", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {os.environ.get('PROXY_API_KEY', '')}"})
    # PROXY_URL is a loopback address (the platform's own listener, or a
    # satellite's tunnel): no proxy may stand between (stop_tracker.py says
    # why). An opener of its own, never install_opener: the network is
    # imported only past the early ends above.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=_TIMEOUT_S) as resp:
            text = json.loads(resp.read().decode("utf-8")).get("text") or ""
    except Exception:  # noqa: BLE001 -- a hook never breaks a start
        return
    if not isinstance(text, str) or not text:
        return
    out = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": text}}
    if not codex:
        # Claude Code: kept off the screen and the transcript view.
        out["suppressOutput"] = True
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
