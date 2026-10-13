"""Codex's per-session config dir — ``.codex`` (``CODEX_HOME``) under a
session's scope root: ``hooks.json`` (the same four events and the same
four scripts as Claude's ``settings.json``, in Codex's schema), the hook
scripts and the stdio interceptor, the skills dir.

The engine's body of what ``core/sandbox/session_config_dir`` keeps generic
(where the dir lives, what every engine installs); reached through
``CodexCLIExecutionLayer.prepare_config_dir``. The ``config.toml`` a session
runs with is written by the layer at spawn (``layer.py``) — this is the
persistent part. The satellite's ``codex_session.py`` writes the same
hooks.json shape from the start payload's scripts.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from core.sandbox import session_config_dir as scd

logger = logging.getLogger("otodock.sandbox")


def build_hooks(sandbox_codex_dir: str) -> dict:
    """The ``hooks.json`` content for the Codex CLI hook system.

    Schema (Codex hooks, per the OpenAI Codex docs):
        {"hooks": {"<Event>": [{"matcher": <regex>,
                                "hooks": [{"type": "command",
                                           "command": <cmd>, "timeout": <s>}]}]}}
    The same four events as Claude's settings.json, the same four scripts
    (docs/architecture/HOOKS.md): Codex passes the same PreToolUse /
    PostToolUse / SubagentStop / Stop stdin shapes and accepts the same
    outputs — the ``hookSpecificOutput`` deny on PreToolUse, the legacy
    ``{"decision": "block", "reason"}`` on Stop (the ONLY shape it accepts
    there) — so the provider-agnostic scripts run unchanged and one
    ``decide_tool_permission`` / ``session_events`` authority serves every
    surface. Runs for INTERACTIVE Codex sessions (``[features] hooks = true``
    + ``--dangerously-bypass-hook-trust``) and for every app-server thread
    (trusted per thread via ``thread/start.config``; ``session.py``): an
    unattended session's gate judges every call, a dashboard chat's only the
    turns Codex runs under ``never`` (Don't Ask, auto), while the JSON-RPC
    approval bridge asks the person in the others (a Codex PreToolUse hook
    cannot say "ask"). Empty matcher = all tools.
    """
    gate = f"{sandbox_codex_dir}/permission_gate.py"
    forwarder = f"{sandbox_codex_dir}/tool_result_forwarder.py"
    subagent = f"{sandbox_codex_dir}/subagent_tracker.py"
    stop = f"{sandbox_codex_dir}/stop_tracker.py"

    def _hook(command: str, timeout: int) -> list[dict]:
        return [{
            "matcher": "",
            "hooks": [{"type": "command", "command": command, "timeout": timeout}],
        }]

    # The interpreter by its resolved path, as Claude's settings name it.
    py = scd.sandbox_python()
    return {
        "hooks": {
            "PreToolUse": _hook(f'{py} -I "{gate}"', scd.LONG_HOOK_TIMEOUT_S),
            "PostToolUse": _hook(f'{py} -I "{forwarder}"', 10),
            "SubagentStop": _hook(f'{py} -I "{subagent}"', 10),
            "Stop": _hook(f'{py} -I "{stop}"', scd.STOP_HOOK_TIMEOUT_S),
        },
    }


def ensure_persistent_codex_dir(
    agent_name: str,
    *,
    username: str = "",
    scope: str = "user",
    external_home=None,
) -> Path:
    """Create or refresh the persistent ``.codex`` dir for a Codex CLI
    session and return its host path (the same scoping as Claude's dir —
    ``session_config_dir.scope_config_dir``). Writes ``hooks.json`` and the
    hook scripts; the skills dir is reconciled (``$CODEX_HOME/skills``;
    Codex's vendored ``.system`` builtins are never touched)."""
    codex_dir, sandbox_codex_dir = scd.scope_config_dir(
        agent_name, ".codex", username=username, scope=scope, external_home=external_home,
    )
    scd.write_if_changed(codex_dir / "hooks.json",
                         (json.dumps(build_hooks(sandbox_codex_dir), indent=2) + "\n").encode())
    scd.install_hook_scripts(codex_dir)

    # Fail-soft; see skills_materializer for the full protocol.
    from core.sandbox.skills_materializer import materialize_skills_for_sandbox
    materialize_skills_for_sandbox(agent_name, codex_dir, username=username)

    os.chmod(codex_dir, 0o700)

    logger.debug(
        f"Prepared .codex/ dir: {codex_dir} "
        f"(agent={agent_name}, user={username or '(none)'})"
    )
    return codex_dir
