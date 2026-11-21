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
    (docs/architecture/HOOKS.md), and Codex's own ``SessionStart``
    (``session_start_hook``): Codex passes the same PreToolUse /
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
    session_text = f"{sandbox_codex_dir}/session_text.py"
    start = f"{sandbox_codex_dir}/start_context.py"

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
            "SessionStart": session_start_hook(f'{py} -I "{session_text}"', f'{py} -I "{start}"'),
        },
    }


def session_start_hook(command: str, start_command: str = "") -> list[dict]:
    """The ``SessionStart`` entry that gives a resumed thread its session's
    text once (``hooks/session_text.py``) and a terminal's start its riding
    part (``hooks/start_context.py``, part 1 of 1). ``additionalContextLimit``
    0: Codex otherwise trims a hook's added text to a 2,500-token preview
    (codex-rs ``hooks/src/output_spill.rs``), and the text is 10k to 35k
    tokens. The satellite's twin writes the same entry."""
    hooks = [{"type": "command", "command": command, "timeout": 10, "additionalContextLimit": 0}]
    if start_command:
        hooks.append({"type": "command", "command": start_command, "timeout": 20,
                      "additionalContextLimit": 0})
    return [{"matcher": "", "hooks": hooks}]


def write_hooks(folder: Path, sandbox_codex_dir: str) -> None:
    """``hooks.json`` in ``folder``, the folder the engine reads (the
    session's home, else the store), naming the store's scripts by
    ``sandbox_codex_dir``."""
    scd.write_if_changed(folder / "hooks.json",
                         (json.dumps(build_hooks(sandbox_codex_dir), indent=2) + "\n").encode())


#: What a session in a home must not find in its store. Codex 0.169 reads the
#: ``.codex`` folder of its working folder as the project's when that folder
#: is not ``CODEX_HOME``: a store's ``config.toml`` and ``hooks.json`` would
#: run every hook twice and add another session's tools. A sign-in or a model
#: catalog there is an earlier session's copy that nothing reads.
STORE_LEFTOVERS = ("config.toml", "hooks.json", "auth.json", "models.json")


def drop_store_leftovers(store: Path) -> None:
    """Remove ``STORE_LEFTOVERS`` from a store a session in a home starts in
    (a 1.9.1 install's, an earlier start's), each beneath the agent's real
    folder with no link followed. On a machine that makes homes every
    session of the store runs from one, so none reads them. An empty
    sign-in stays: it is the mount point a live sandbox holds its mask over
    (``sandbox_render.home_args``), and removing it would detach that mask."""
    import stat

    from core.agent_folder import beneath_agent
    from core.layers.codex import session_text
    from services.infra import safe_fs
    for name in STORE_LEFTOVERS:
        where = beneath_agent(store / name)
        if name == "auth.json":
            try:
                st = safe_fs.lstat_beneath(*where)
            except FileNotFoundError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_size == 0:
                continue
        safe_fs.unlink_beneath(*where, missing_ok=True)
    session_text.remove_shared_text(store)


def ensure_persistent_codex_dir(
    agent_name: str,
    *,
    username: str = "",
    scope: str = "user",
    external_home=None,
    outside_picks=None,
    kind_rule=None,
) -> Path:
    """Create or refresh the persistent ``.codex`` dir for a Codex CLI
    session and return its host path (the same scoping as Claude's dir —
    ``session_config_dir.scope_config_dir``). Writes the hook scripts; the
    skills dir is reconciled (``$CODEX_HOME/skills``; Codex's vendored
    ``.system`` builtins are never touched). ``hooks.json`` is the start's
    (``write_hooks``), in the folder the engine reads: a session's home
    when it has one, so the store holds none a terminal would read as its
    project's."""
    codex_dir, _sandbox_codex_dir = scd.scope_config_dir(
        agent_name, ".codex", username=username, scope=scope, external_home=external_home,
    )
    scd.install_hook_scripts(codex_dir)

    # Fail-soft; see skills_materializer for the full protocol.
    from core.sandbox.skills_materializer import materialize_skills_for_sandbox
    materialize_skills_for_sandbox(agent_name, codex_dir, username=username,
                                   outside_picks=outside_picks, kind_rule=kind_rule)

    scd.restrict_dir(agent_name, codex_dir)

    logger.debug(
        "Prepared .codex/ dir: %s "
        "(agent=%s, user=%s)",
        codex_dir, agent_name, username or '(none)'
    )
    return codex_dir
