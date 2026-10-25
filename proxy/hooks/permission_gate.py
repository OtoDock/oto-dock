#!/usr/bin/env python3
"""PreToolUse hook: calls the proxy to decide whether to allow or deny a tool use.

The proxy decides based on session mode and client type:
  - the unattended "auto" mode: allow (phone, tasks), the floor still refuses
  - "ask" + dashboard: block until the person approves/denies in the dashboard
  - "autoReview" (Auto): its allow set runs; the rest is the engine's own
    reviewer's ("defer": this script prints nothing and the engine decides)
  - "plan" mode, every client: read-only tools, the read tier of the shell and
    plan-file writes run; every other tool is denied (read-only planning)
  - AskUserQuestion: always deny (question shown to user, they reply in next message)

Environment variables (set by core/sandbox/env_builder.py in the CLI's subprocess env):
  PROXY_URL         - e.g. http://127.0.0.1:8400
  PROXY_API_KEY     - Bearer token for auth
  OTO_SESSION_ID - session UUID for this conversation
"""

import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request

# Transport failures fail CLOSED. The alternative — silently allowing the
# tool a human was being asked to approve — turns every proxy restart or
# tunnel drop into an auto-approval. While the platform cannot be reached (a
# restart, a dropped tunnel: the satellite's loopback tunnel answers 502/503
# at once while its socket is down) the call is held and retried for up to
# _HOLD_S, then denied; any answer from the platform ends the hold at once.
_HOLD_S = 120.0
_BACKOFF = (2.0, 4.0, 8.0, 15.0)  # then 15 s steps until the hold ends
# A refused session token right after a restart: the satellite reports its
# sessions a moment after it reconnects. A short ladder, then a deny.
_AUTH_LADDER = (2.0, 4.0, 8.0)
# A decision that waits on a person is streamed with whitespace keepalives
# every 30 s, so a few intervals of silence mean the stream is gone.
_SOCKET_TIMEOUT_S = 150
# The permission_mode values Codex reports for a turn under an asking
# approval policy ("default"; "bypassPermissions" means never). A satellite
# before 1.9 runs its attended Codex chats on the retired "untrusted"
# policy with OTO_HOOK_BRIDGED set: every command reaches its bridge, so
# the gate stands aside on such a turn. Nothing else sets the switch.
_BRIDGE_ASKS = frozenset({"default", "acceptEdits", "plan"})


def _request_decision(req):
    """POST to the proxy. The decision dict; a deny for a definitive refusal;
    None when the platform stayed unreachable for the whole hold, counted
    from the first failure (a prompt may wait days before its stream drops)."""
    deadline = None
    unreachable = auth = 0
    while True:
        try:
            with urllib.request.urlopen(req, timeout=_SOCKET_TIMEOUT_S) as resp:
                text = resp.read().decode("utf-8", "replace").strip()
            if text:
                return json.loads(text)
            # A 2xx whose body ended before its JSON: the stream dropped.
        except urllib.error.HTTPError as e:
            if e.code == 401 and auth < len(_AUTH_LADDER):
                time.sleep(_AUTH_LADDER[auth])
                auth += 1
                continue
            if e.code not in (502, 503, 504):
                return {"decision": "deny",
                        "reason": f"OtoDock refused this tool call (HTTP {e.code})"}
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError):
            pass  # unreachable, or a streamed answer cut before its JSON
        wait = _BACKOFF[min(unreachable, len(_BACKOFF) - 1)]
        if deadline is None:
            deadline = time.monotonic() + _HOLD_S
        if time.monotonic() + wait > deadline:
            return None
        time.sleep(wait)
        unreachable += 1


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
    except Exception:  # noqa: BLE001  # the crash breadcrumb must not stop the fail-closed deny after it
        pass


def main():
    # Read hook input from stdin. OSError: Windows pipe edge cases — treat an
    # unreadable stdin like an empty payload rather than crashing the hook.
    try:
        inp = json.loads(sys.stdin.read())
    except (OSError, ValueError):
        inp = {}

    # An attended Codex chat on a satellite before 1.9 (OTO_HOOK_BRIDGED):
    # its approval requests carry the person's cards, so the gate stands
    # aside on a turn Codex says will raise them, and judges every other one
    # (Don't Ask and auto run under never: "bypassPermissions"). An unknown
    # or missing value is judged: a turn with no judge is the failure to avoid.
    if os.environ.get("OTO_HOOK_BRIDGED") and inp.get("permission_mode") in _BRIDGE_ASKS:
        return

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
        # This host's clock, the one the CLI's transcript lines carry: a
        # terminal's turn parked on a dialog ignores lines journaled before.
        "hook_sent_at": time.time(),
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

    # "defer" = the platform has NO OPINION (a terminal's residual tier,
    # Auto's review set): emit no decision, so the engine's own permission
    # engine governs the call (the live Shift+Tab mode the human chose, the
    # CLI's classifier in its auto mode, Codex's reviewer). Distinct from
    # "allow", which is a hard override that skips the engine's own check.
    # A satellite path rewrite still rides it as a bare updatedInput (Claude
    # Code applies it and judges the corrected call; Codex takes none).
    # Denies, asks and allows still emit below.
    if decision == "defer":
        if isinstance(updated_input, dict) and not os.environ.get("OTO_HOOK_DENY_ONLY"):
            output = {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "updatedInput": updated_input}}
            note = _path_note(tool_input, updated_input)
            if note:
                output["hookSpecificOutput"]["additionalContext"] = note
            print(json.dumps(output))
        return

    # Codex's PreToolUse hook supports a DENY decision but REJECTS
    # permissionDecision:"allow" ("unsupported permissionDecision:allow"). Under
    # Codex (OTO_HOOK_DENY_ONLY=1, set on every Codex spawn) emit JSON only to
    # DENY; any non-deny (allow/ask) becomes "no opinion" (exit 0) → the CLI
    # proceeds and Codex's own sandbox / -a on-request handles any prompt. A
    # platform card the decision waited on was answered before this point.
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
    except Exception:  # noqa: BLE001  # the gate answers deny on any crash, a traceback lets the tool run
        # A hook crash enforces nothing on its own: both CLIs read a non-zero
        # exit as a non-blocking error and run the tool, and Claude Code's
        # onFailure block (the platform's settings, 2.1.295+) cannot see an
        # exit 0 with no output. So the crash denies in the gate's own JSON,
        # which both CLIs honour (Codex accepts a deny), after a breadcrumb.
        # The proxy-unreachable deny stays in main.
        _log_failure()
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                "OtoDock permission gate error — tool call denied (fail closed)"
            ),
        }}))
        sys.exit(0)
