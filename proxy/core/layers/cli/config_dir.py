"""The Claude Code CLI's per-session config dir — ``.claude`` under a
session's scope root: ``settings.json`` (the platform's four hooks, the
built-in tools the platform denies, the session's own denials), the hook
scripts and the stdio interceptor, the skills dir.

The engine's body of what ``core/sandbox/session_config_dir`` keeps generic
(where the dir lives, what every engine installs). Reached through
``CLIExecutionLayer.prepare_config_dir``; two other callers ask for THIS
tree by name and say why where they do — the app buttons' headless exec and
Direct LLM, which keep their plans dir and in-process MCP config home in it.
Never imports ``layer.py`` (the package ``__init__`` would close a cycle).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

from core.sandbox import session_config_dir as scd

logger = logging.getLogger("claude-proxy.sandbox")


# Claude Code CLI built-in tools that are denied on this platform.
#
# These tools either:
#   (a) reach the user's claude.ai personal account (Cron*, RemoteTrigger,
#       PushNotification, mcp__claude_ai_*) — agents on this platform must
#       not act on the user's claude.ai account.
#   (b) collide with platform features (RemoteTrigger ↔ our triggers,
#       Cron* ↔ our schedules, PushNotification ↔ our notifications).
#   (c) are server-side context irrelevant (ScheduleWakeup is for Claude
#       Code's local /loop dynamic mode).
#
# The platform's own equivalents (tasks, schedules, notifications,
# triggers, google-workspace MCP) replace each of these with a per-user
# permissioned, scoped version. The Task* family (TaskCreate / TaskGet /
# TaskList / TaskUpdate / TaskOutput / TaskStop) is intentionally KEPT —
# those are Claude Code's session-internal todo tracking, useful and
# distinct from our persistent task system. Shipped to a satellite as the
# start payload's ``disallowed_tools`` (``remote.py``), so a remote session
# denies the same set.
DISALLOWED_BUILTIN_TOOLS = [
    # Claude.ai cron jobs (collides with our schedules)
    "CronCreate",
    "CronDelete",
    "CronList",
    # Claude.ai webhook triggers (collides with our triggers)
    "RemoteTrigger",
    # Claude.ai push notifications (collides with our notifications)
    "PushNotification",
    # /loop dynamic-mode helper — server-side agent context, irrelevant
    "ScheduleWakeup",
    # Claude.ai personal-account integrations — we have our own
    # google-workspace MCP with per-user OAuth on the platform
    "mcp__claude_ai_Gmail__authenticate",
    "mcp__claude_ai_Gmail__complete_authentication",
    "mcp__claude_ai_Google_Calendar__authenticate",
    "mcp__claude_ai_Google_Calendar__complete_authentication",
    "mcp__claude_ai_Google_Drive__authenticate",
    "mcp__claude_ai_Google_Drive__complete_authentication",
    # "Skill" was denied here until 2026-07 ("parallel memory path"). It is
    # now ALLOWED: platform-managed on-demand skills are materialized into
    # .claude/skills/ (skills_materializer) and the Skill tool is their
    # activation surface. The no-parallel-memory guarantee moved from tool
    # denial to reconciliation — agent-written skill content is reverted /
    # quarantined at every session start. Unmanaged skill SOURCES stay
    # closed elsewhere: bundled CLI skills via
    # CLAUDE_CODE_DISABLE_BUNDLED_SKILLS=1 (env_builder + satellite spawn),
    # plugin skills via the "enabledPlugins" entries below.
    # Claude.ai artifacts: the tool publishes to the signed-in claude.ai
    # account, which on a pool login is the pool owner's; the platform's
    # apps and display artifacts are the in-platform equivalent.
    "Artifact",
    "ArtifactComments",
    "ArtifactData",
    "ArtifactCheck",
]

# Claude Code's built-in plugins (2.1.287 and later ship "mods": plugins
# whose handlers can change the CLI's behaviour and run ahead of a
# non-managed PreToolUse hook). An empty ``enabledPlugins`` map leaves the
# built-ins ON (verified: ``cc-plugin-plugin-authoring`` loaded under the
# platform's settings and its skill reached the session), and a remote
# feature flag can switch on a built-in that shipped dark, so only the four
# the platform wants stay on — ``cc-plugin-agents-md`` (AGENTS.md),
# ``cc-plugin-diff`` (/diff), ``cc-plugin-telemetry`` and the managed-only
# ``cc-plugin-sec-default`` (the deny-first guard) — and every other built-in
# the binary names is off by name, so no flag can add a skill, an agent, a
# command or a reply-mod. ``agents-md`` and ``telemetry`` load on 2.1.281
# too; older CLIs ignore an id they do not know. Satellite twin:
# ``satellite/sessions/cli_session.py``, held equal by test_sandbox.
DISABLED_BUILTIN_PLUGINS = (
    "cc-plugin-plugin-authoring@builtin",
    "cc-plugin-you-should-know@builtin",
    "cc-plugin-claude-test@builtin",
    "cc-plugin-mods-guide@builtin",
    "cc-plugin-mermaid@builtin",
    "cc-plugin-responsive-mode@builtin",
    "cc-plugin-tips@builtin",
)


def build_settings(sandbox_claude_dir: str) -> dict:
    """The ``settings.json`` a session runs with, on sandbox-internal paths.

    ``sandbox_claude_dir`` is the sandbox-internal ``.claude`` path
    (``/users/alice/.claude``, ``/caller/.claude`` or ``/workspace/.claude``).
    The file is the scope's, shared by every session of that scope, and the
    CLI reloads it live, so it carries only the platform-wide deny list: a
    session's own denials (an external caller's shell, a judge's write
    tools) ride its argv and the hook floor, or a judge would take the write
    tools off the judged person's live chats.

    The "sandbox" block disables Claude Code's own bwrap layer: the platform
    already wraps the CLI in a bwrap of its own (SandboxBuilder), so the
    inner sandbox is redundant and has caused nested-namespace failures in
    2.1.x. failIfUnavailable=False keeps the CLI from refusing to start if a
    future build flips enabled back on and the inner sandbox can't
    initialise.
    """
    # Each hook runs the sandbox's python in isolated mode (no PYTHON*
    # variable, no user site, no script directory on the path), so nothing
    # the agent sets in its environment changes what a hook executes.
    gate = f'python3 -I "{sandbox_claude_dir}/permission_gate.py"'
    forwarder = f'python3 -I "{sandbox_claude_dir}/tool_result_forwarder.py"'
    subagent = f'python3 -I "{sandbox_claude_dir}/subagent_tracker.py"'
    stop = f'python3 -I "{sandbox_claude_dir}/stop_tracker.py"'

    return {
        "sandbox": {
            "enabled": False,
            "failIfUnavailable": False,
        },
        # Disable Claude Code's built-in auto-memory subsystem. The platform's
        # otodock memory (topic files under knowledge/memory/ +
        # users/{u}/context/memory/, injected by the prompt-builder, written via
        # memory-mcp) is the single source of memory truth — having Claude
        # Code's ``/memory`` slash command + auto-import of
        # ``.claude/projects/{cwd}/memory/MEMORY.md`` running in parallel would
        # split the agent's view across two uncoordinated stores.
        # ``autoMemoryEnabled: false`` keeps ``CLAUDE.md`` import working (we
        # don't ship one anyway) but turns off auto-memory specifically.
        # Belt-and-braces: ``ensure_persistent_claude_dir`` also wipes the
        # memory subdir at session start, and ``env_builder`` injects
        # ``CLAUDE_CODE_DISABLE_AUTO_MEMORY=1``.
        "autoMemoryEnabled": False,
        # Pin the CLI version fleet-wide: disable Claude Code's own
        # auto-updater so an install can't drift off the platform pin (the
        # satellite reconciles the pinned version). Belt-and-braces with env
        # DISABLE_AUTOUPDATER=1 (env_builder).
        "autoUpdates": False,
        # The platform is the only skill SOURCE: with the Skill tool
        # allowed, plugin skills must not activate outside
        # install/approval/version-pinning. This settings.json is rewritten
        # every session start, so plugin enablement is platform-owned state:
        # no marketplace plugin is enabled, and every built-in but the four
        # the platform keeps is off by name (DISABLED_BUILTIN_PLUGINS). The
        # spawns read this file alone (--setting-sources user), so a plugin
        # enabled from a terminal at the local scope never outranks it. Live
        # installs carry auto-installed marketplace trees under
        # .claude/plugins/; those stay on disk (inert).
        "enabledPlugins": {plugin: False for plugin in DISABLED_BUILTIN_PLUGINS},
        # Claude Code ≥ 2.1.275 syncs the skills and plugins enabled on the
        # signed-in claude.ai account into the session. Platform sessions run
        # on pool accounts (CLAUDE_CODE_OAUTH_TOKEN): a pool owner's personal
        # skills must not reach every user's agent, and the platform is the
        # only skill source (the materialized .claude/skills/). Both off;
        # older CLIs ignore the keys (probed on 2.1.263). Satellite twin:
        # satellite/sessions/cli_session.py::_write_cli_hooks.
        "syncClaudeAiSkills": False,
        "syncClaudeAiPlugins": False,
        "permissions": {
            "deny": list(DISALLOWED_BUILTIN_TOOLS),
        },
        "hooks": {
            "PreToolUse": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": gate,
                    "timeout": scd.LONG_HOOK_TIMEOUT_S,
                }],
            }],
            "PostToolUse": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": forwarder,
                    "timeout": 10,
                }],
            }],
            # Deterministic, idle-safe subagent completion (fg + bg). Fires
            # when a subagent stops — drives the SubagentRegistry completion
            # gate without polling stdout. See hooks/subagent_tracker.py.
            "SubagentStop": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": subagent,
                    "timeout": 10,
                }],
            }],
            # Turn end: transcript persistence for the interactive TUI (no
            # pump) and the platform's turn_end verdict, which may hold the
            # turn open with a reason (HOOKS.md). The week-long timeout is
            # the gate's: a verdict may wait on a check; the proxy bounds
            # the wait itself. See hooks/stop_tracker.py.
            "Stop": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": stop,
                    "timeout": scd.STOP_HOOK_TIMEOUT_S,
                }],
            }],
        }
    }


def ensure_persistent_claude_dir(
    agent_name: str,
    *,
    username: str = "",
    scope: str = "user",
    external_home=None,
) -> Path:
    """Create or refresh the persistent ``.claude`` dir for a session and
    return its host path (``session_config_dir.scope_config_dir`` says
    where: the user's tree, the agent workspace, or an external caller's
    private tree, mounted at ``/caller/.claude``).

    Writes ``settings.json`` (the hooks, the platform-wide deny list) and the
    hook scripts. Plans and session data the CLI creates are left untouched.
    """
    claude_dir, sandbox_claude_dir = scd.scope_config_dir(
        agent_name, ".claude", username=username, scope=scope, external_home=external_home,
    )

    # Defensive cleanup: Claude Code CLI's built-in auto-memory (slash
    # ``/memory`` command + auto-imports from ``MEMORY.md``) writes to
    # ``.claude/projects/{cwd-encoded}/memory/`` and runs PARALLEL to our
    # otodock memory system (topic files under ``knowledge/memory/`` +
    # ``users/{u}/context/memory/``). Two coexisting memory systems confuses
    # the LLM (it doesn't know which is canonical) and persists facts the
    # platform never gates. Wipe each session start so otodock-memory is
    # the only durable memory the agent sees. Matches the Codex pattern
    # (``.codex/memories/`` wipe in ``close_codex_session``). Session
    # JSONLs (sibling files in ``projects/{id}/``) are left untouched —
    # only the ``memory/`` subdir is removed.
    projects_dir = claude_dir / "projects"
    if projects_dir.exists():
        for proj in projects_dir.iterdir():
            if not proj.is_dir():
                continue
            mem_dir = proj / "memory"
            if mem_dir.exists():
                shutil.rmtree(mem_dir, ignore_errors=True)

    settings = build_settings(sandbox_claude_dir)
    scd.write_no_follow(claude_dir / "settings.json",
                        (json.dumps(settings, indent=2) + "\n").encode())
    scd.install_hook_scripts(claude_dir)

    # Reconcile the platform-managed on-demand skills dir (fail-soft — a
    # skills problem must never block a session start). See
    # skills_materializer for the full protocol.
    from core.sandbox.skills_materializer import materialize_skills_for_sandbox
    materialize_skills_for_sandbox(agent_name, claude_dir, username=username)

    os.chmod(claude_dir, 0o700)

    logger.debug(
        f"Prepared .claude/ dir: {claude_dir} "
        f"(agent={agent_name}, user={username or '(none)'})"
    )
    return claude_dir
