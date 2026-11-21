#!/usr/bin/env python3
"""Status line command: tells the proxy what the engine read about its
account's usage windows, and draws nothing.

Claude Code runs a status line command after each answer of the model and at
its redraws, with JSON on stdin; in a terminal that JSON carries
``rate_limits`` (``five_hour`` and ``seven_day``, each a ``used_percentage``
and an epoch ``resets_at``), which the engine read from the vendor's own
answer. A headless run never calls it. Named in every ``settings.json`` the
platform and a satellite write (``core/layers/cli/config_dir.py``,
``satellite/sessions/cli_session.py``).

One POST to ``/v1/hooks/usage-report``, and only when the numbers differ from
the last ones this session posted (kept in the temporary folder, per session).
The proxy files the report as a hint on the session's own account
(``api/hooks/usage.py``). Prints nothing, so the terminal shows no status
line. Same shape as the other scripts: stdlib only, exit 0 always.

Environment variables (set by the proxy / satellite via subprocess env,
inherited by the command):
  PROXY_URL         - e.g. http://127.0.0.1:8400
  PROXY_API_KEY     - Bearer token for auth
  OTO_SESSION_ID    - session UUID for this conversation
"""

import hashlib
import http.client
import json
import os
import sys
import tempfile
import urllib.request

_TIMEOUT_S = 5
_KEYS = ("five_hour", "seven_day")
_FIELDS = ("used_percentage", "resets_at")


def _report(inp):
    """The windows to post, numbers only, or None when the input has none."""
    limits = inp.get("rate_limits") if isinstance(inp, dict) else None
    if not isinstance(limits, dict):
        return None
    out = {}
    for key in _KEYS:
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        kept = {f: window[f] for f in _FIELDS
                if isinstance(window.get(f), (int, float)) and not isinstance(window.get(f), bool)}
        if "used_percentage" in kept:
            out[key] = kept
    return out or None


def _last_path(session_id):
    name = hashlib.sha256(session_id.encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"otodock-usage-{name}.json")


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

    report = _report(inp)
    if report is None:
        return
    body = json.dumps({"session_id": session_id, "rate_limits": report}, sort_keys=True)
    last = _last_path(session_id)
    try:
        with open(last, encoding="utf-8") as f:
            if f.read() == body:
                return
    except OSError:
        pass

    req = urllib.request.Request(
        f"{proxy_url}/v1/hooks/usage-report",
        data=body.encode(),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            resp.read()
    except (OSError, http.client.HTTPException, ValueError):
        return  # a refused or slow report is dropped; the next answer reports again
    try:
        # A link planted at the name in a shared temporary folder is refused,
        # and the note is the owner's alone.
        fd = os.open(last, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
    except OSError:
        pass


if __name__ == "__main__":
    # PROXY_URL is a loopback address (the platform's own listener, or a
    # satellite's tunnel): no proxy may stand between (stop_tracker.py says
    # why). Installed here, never at import: a test that loads the script
    # keeps its own opener.
    urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
    try:
        main()
    except Exception:  # noqa: BLE001  # a status line command exits 0 whatever failed, it draws nothing
        sys.exit(0)
