"""The environment a Codex app-server daemon starts with."""

from __future__ import annotations

import os


def daemon_env(session_id: str, agent_name: str, *, username: str, user_role: str,
               codex_bin: str, extra_env: dict[str, str]) -> dict[str, str]:
    """The app-server daemon's environment: the session env, the codex
    binary's folder on PATH (systemd has a minimal PATH), the subscription
    and credential variables, the hook words and, last, the shell's
    command-line token: the environment of the spawn plan a machine runs."""
    from core.sandbox.env_builder import build_session_env
    env = build_session_env(session_id, agent_name, username=username, user_role=user_role)
    codex_dir = os.path.dirname(os.path.realpath(codex_bin))
    if codex_dir and codex_dir not in env.get("PATH", ""):
        env["PATH"] = codex_dir + ":" + env.get("PATH", "/usr/bin:/bin")
    env.update(extra_env)  # subscription auth (CODEX_HOME, etc.)
    # Codex's PreToolUse hook rejects permissionDecision:"allow" —
    # permission_gate.py emits JSON only to DENY; and the PostToolUse
    # forwarder would double every tool card the JSON-RPC stream
    # already renders, so it exits early. Hooks inherit this env.
    env["OTO_HOOK_DENY_ONLY"] = "1"
    env["OTO_HOOK_NO_FORWARD"] = "1"
    # ``less`` is a read, so it starts with no card, and Codex calls no
    # hook for what is then typed into it: its shell and editor escapes
    # are off. A guard for the one pager, never the boundary.
    env["LESSSECURE"] = "1"
    # The gate judges every call of every session (an attended chat's
    # too: the bridge takes only what crosses the sandbox). The flag is
    # popped so a value an older release set never reaches the child.
    env.pop("OTO_HOOK_BRIDGED", None)
    # Last, over the final session token: the shell's command-line token.
    from auth.session_token import attach_cli_token
    attach_cli_token(env)
    return env
