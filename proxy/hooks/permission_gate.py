#!/usr/bin/env python3
"""PreToolUse hook: calls the proxy to decide whether to allow or deny a tool use.

The proxy decides based on session mode and client type:
  - "auto" mode: always allow (phone, tasks)
  - "default" mode + dashboard: block until user approves/denies in dashboard UI
  - "plan" mode: always deny (read-only planning)
  - AskUserQuestion: always deny (question shown to user, they reply in next message)

Environment variables (set by core/sandbox/env_builder.py in the CLI's subprocess env):
  PROXY_URL         - e.g. http://127.0.0.1:8400
  PROXY_API_KEY     - Bearer token for auth
  OTO_SESSION_ID - session UUID for this conversation
"""

import json
import os
import sys
import time
import urllib.request

# Transport failures fail CLOSED. The alternative — silently allowing the
# tool a human was being asked to approve — turns every proxy restart or
# tunnel drop into an auto-approval. The short retry ladder rides out a
# proxy reboot; the satellite loopback tunnel answers fast with synthetic
# 502/503 once its WS is down, so a dead platform never hangs the CLI here.
_RETRY_DELAYS = (2.0, 4.0)


def _request_decision(req):
    """POST to the proxy, retrying transient failures. None = unreachable."""
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            with urllib.request.urlopen(req, timeout=604800) as resp:
                return json.loads(resp.read())
        except Exception:
            if attempt < len(_RETRY_DELAYS):
                time.sleep(_RETRY_DELAYS[attempt])
    return None


def _path_note(tool_input, updated_input):
    """One-line additionalContext teaching the model the native path mapping.

    Edit / NotebookEdit validate their target BEFORE PreToolUse hooks run, so
    a sandbox-virtual path there fails with file-not-found and no rewrite can
    intervene — the note hands the model the native form at the moment it
    learns the mapping, steering its later edits to paths that work.
    """
    for key, new in updated_input.items():
        old = tool_input.get(key)
        if isinstance(new, str) and isinstance(old, str) and old != new:
            return (
                f"OtoDock path note: `{old}` is `{new}` on this machine. "
                "Path auto-translation covers Read/Write/Glob/Grep only — "
                "the Edit and NotebookEdit tools check their target before "
                "translation runs, so pass them the OS-native form (like "
                "the resolved path above) directly."
            )
    return ""


def _log_failure():
    """Best-effort breadcrumb for a hook crash (see __main__ handler)."""
    try:
        import traceback
        path = os.environ.get("OTO_HOOK_LOG") or os.path.join(
            os.path.expanduser("~"), ".oto-dock", "hook-error.log"
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"--- {time.strftime('%Y-%m-%dT%H:%M:%S')} pid={os.getpid()}\n")
            traceback.print_exc(file=fh)
    except Exception:
        pass


def main():
    # Read hook input from stdin. OSError: Windows pipe edge cases — treat an
    # unreadable stdin like an empty payload rather than crashing the hook.
    try:
        inp = json.loads(sys.stdin.read())
    except (OSError, ValueError):
        inp = {}

    session_id = os.environ.get("OTO_SESSION_ID", "")
    proxy_url = os.environ.get("PROXY_URL", "")
    api_key = os.environ.get("PROXY_API_KEY", "")

    # If env vars aren't set (e.g. subagent call), allow by default
    if not proxy_url or not api_key:
        return

    tool_name = inp.get("tool_name", "")
    tool_input = inp.get("tool_input", {})

    payload = json.dumps({
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        # LIVE mode from the CLI (reflects in-TUI Shift+Tab) — the proxy uses
        # it as the effective mode for interactive sessions.
        "permission_mode": inp.get("permission_mode", ""),
    }).encode()

    req = urllib.request.Request(
        f"{proxy_url}/v1/hooks/permission",
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )

    result = _request_decision(req)
    if result is None:
        decision = "deny"
        reason = "OtoDock platform unreachable — tool call denied (fail closed)"
        updated_input = None
    else:
        decision = result.get("decision", "allow")
        reason = result.get("reason", "")
        updated_input = result.get("updated_input")

    # "defer" = the platform has NO OPINION (interactive residual tier):
    # emit nothing, so the CLI's own permission engine — the live Shift+Tab
    # mode the human actually chose — governs the call. Distinct from
    # "allow", which is a hard override that would SUPPRESS the CLI's own
    # prompt. Denies, asks and allows still emit below.
    if decision == "defer":
        return

    # Codex's PreToolUse hook supports a DENY decision but REJECTS
    # permissionDecision:"allow" ("unsupported permissionDecision:allow"). Under
    # Codex (OTO_HOOK_DENY_ONLY=1, set on the interactive spawn) emit JSON only to
    # DENY; any non-deny (allow/ask) becomes "no opinion" (exit 0) → the CLI
    # proceeds and Codex's own sandbox / -a on-request handles any prompt.
    # Claude supports "allow"/"ask" and needs them, so this gate is Codex-only.
    if decision != "deny" and os.environ.get("OTO_HOOK_DENY_ONLY"):
        return

    # Output in Claude Code's expected hookSpecificOutput format
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
        }
    }
    if reason:
        output["hookSpecificOutput"]["permissionDecisionReason"] = reason
    if decision == "deny" and not reason:
        output["hookSpecificOutput"]["permissionDecisionReason"] = "Denied by user"
    # Remote satellites: the proxy rewrote a sandbox-virtual / `~` path arg to
    # its satellite-host form — hand the CLI the corrected input so the tool
    # runs against the real path (deny never rewrites). On "ask" the CLI
    # prompts against — and then runs with — the corrected input.
    if decision in ("allow", "ask") and isinstance(updated_input, dict):
        output["hookSpecificOutput"]["updatedInput"] = updated_input
        note = _path_note(tool_input, updated_input)
        if note:
            output["hookSpecificOutput"]["additionalContext"] = note

    print(json.dumps(output))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # A hook crash enforces nothing: both CLIs treat a non-zero exit as a
        # non-blocking error and run the tool anyway, so dying loudly only adds
        # "hook exited with code 1" noise (seen on a Windows satellite) while
        # silently dropping the deny floor for that call. Leave a breadcrumb
        # and exit 0. The fail-closed properties live in the JSON path — proxy
        # unreachable still emits a deny — and are unaffected.
        _log_failure()
        sys.exit(0)
