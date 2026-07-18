"""Per-session config-directory setup for agent sandboxes.

Builds and populates the persistent on-disk config directories a session needs
inside the bwrap sandbox: the ``.claude``/``.codex`` dirs (hooks, settings.json,
the stdio interceptor), the agent config dir, and the sandbox-side MCP config.
Split out of sandbox.py; sandbox.py re-exports the public entry points so
existing `from core.sandbox.sandbox import ensure_persistent_*` imports keep working.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path

import config as app_config

logger = logging.getLogger("claude-proxy.sandbox")


# Hooks source directory (proxy/hooks/)
_HOOKS_DIR = app_config.BASE_DIR / "hooks"

# The stdio interceptor lives at core/ (one level above this subpackage —
# it must stay there: the satellite vendors it by sha256 from that path). It
# is copied into each session's .claude/.codex dir (like the hooks) so it is
# reachable INSIDE the bwrap sandbox — stdlib-only, run via the sandbox's
# `python3`. Used by the credential broker (fetch-at-spawn) + tool-arg-path
# translation.
_INTERCEPTOR_SRC = Path(__file__).resolve().parent.parent / "stdio_path_interceptor.py"


def _copy_hook_lf(src: Path, dst: Path) -> None:
    """Copy a hook/interceptor script into a sandbox, normalizing to LF and
    forcing the executable bit. A stray CR in the shebang
    (``#!/usr/bin/env python3\\r``) makes the kernel look for an interpreter
    literally named ``python3\\r`` → the hook fails to start
    (``/usr/bin/env: 'python3\\r': No such file or directory``) and silently
    bypasses enforcement. Normalize defensively so an editor/git CRLF can never
    break hook execution inside the sandbox (vs ``shutil.copy2``, which copies
    bytes + perms verbatim)."""
    dst.write_bytes(src.read_bytes().replace(b"\r\n", b"\n"))
    os.chmod(dst, 0o755)


# ---------------------------------------------------------------------------
# Persistent .claude/ directory management
# ---------------------------------------------------------------------------

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
# distinct from our persistent task system.
_DISALLOWED_BUILTIN_TOOLS = [
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
    # plugin skills via "enabledPlugins": {} below.
]


def _build_sandbox_cli_settings(sandbox_claude_dir: str) -> dict:
    """Build settings.json with sandbox-internal paths.

    sandbox_claude_dir is the sandbox-internal .claude/ path,
    e.g. /users/alice/.claude or /workspace/.claude.

    The "sandbox" block disables Claude Code's own bwrap layer:
    the platform already wraps the CLI in a bwrap of its own (see
    SandboxBuilder), so the inner sandbox is redundant and has caused
    nested-namespace failures in 2.1.x. failIfUnavailable=False keeps
    the CLI from refusing to start if a future build flips enabled
    back on and the inner sandbox can't initialise.
    """
    gate = f"{sandbox_claude_dir}/permission_gate.py"
    forwarder = f"{sandbox_claude_dir}/tool_result_forwarder.py"
    subagent = f"{sandbox_claude_dir}/subagent_tracker.py"
    stop = f"{sandbox_claude_dir}/stop_tracker.py"

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
        # The platform is the only skill SOURCE: with
        # the Skill tool allowed, plugin skills must not activate outside
        # install/approval/version-pinning. This settings.json is rewritten
        # every session start, so plugin enablement is platform-owned state —
        # an explicit empty map keeps every plugin off. Live installs carry
        # auto-installed marketplace trees under .claude/plugins/; those stay
        # on disk (inert). VERIFY at dogfood: no plugin skills in the CLI's
        # skills index (pre-impl checklist item 1, plan §checklist).
        "enabledPlugins": {},
        "permissions": {
            "deny": list(_DISALLOWED_BUILTIN_TOOLS),
        },
        "hooks": {
            "PreToolUse": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": gate,
                    "timeout": 604800,
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
            # Turn-end signal for INTERACTIVE sessions (no pump) → transcript
            # persistence; no-ops for headless -p. See hooks/stop_tracker.py.
            "Stop": [{
                "matcher": "",
                "hooks": [{
                    "type": "command",
                    "command": stop,
                    "timeout": 30,
                }],
            }],
        }
    }


def ensure_persistent_claude_dir(
    agent_name: str,
    *,
    username: str = "",
    scope: str = "user",
) -> Path:
    """Create/update the persistent .claude/ dir for a session.

    Determines host path based on scope:
    - User session: agents/{agent}/users/{username}/.claude/
    - Agent-scoped task: agents/{agent}/workspace/.claude/

    Writes/overwrites settings.json and hook scripts. Plans and session
    data that Claude CLI creates are left untouched (persistent).

    Returns the host path to the .claude/ directory.
    """
    agent_dir = app_config.get_agent_dir(agent_name)

    if username and scope == "user":
        claude_dir = agent_dir / "users" / username / ".claude"
    else:
        claude_dir = agent_dir / "workspace" / ".claude"

    claude_dir.mkdir(parents=True, exist_ok=True)

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

    # Write settings.json (hooks config) — always sandbox-internal paths
    if username and scope == "user":
        sandbox_claude_dir = f"/users/{username}/.claude"
    else:
        sandbox_claude_dir = "/workspace/.claude"
    settings = _build_sandbox_cli_settings(sandbox_claude_dir)

    (claude_dir / "settings.json").write_text(
        json.dumps(settings, indent=2) + "\n"
    )

    # Copy hook scripts into .claude/ dir (LF-normalized + executable — see
    # _copy_hook_lf; a CRLF shebang silently breaks hook execution).
    for script_name in ("permission_gate.py", "tool_result_forwarder.py",
                        "subagent_tracker.py", "stop_tracker.py"):
        src = _HOOKS_DIR / script_name
        dst = claude_dir / script_name
        if src.exists():
            _copy_hook_lf(src, dst)

    # Copy the stdio interceptor alongside the hooks so it is reachable inside
    # the bwrap sandbox (credential-broker fetch-at-spawn + tool-arg-path
    # translation). Stdlib-only → runs via the sandbox `python3`. Copied
    # unconditionally: a missing source must raise, not silently disable the
    # interceptor wrap that spawn-time config still points at.
    _copy_hook_lf(_INTERCEPTOR_SRC, claude_dir / _INTERCEPTOR_SRC.name)

    # Reconcile the platform-managed on-demand skills dir (fail-soft — a
    # skills problem must never block a session start). See
    # skills_materializer for the full protocol.
    from core.sandbox.skills_materializer import materialize_skills_for_sandbox
    materialize_skills_for_sandbox(agent_name, claude_dir)

    os.chmod(claude_dir, 0o700)

    logger.debug(
        f"Prepared .claude/ dir: {claude_dir} "
        f"(agent={agent_name}, user={username or '(none)'})"
    )
    return claude_dir


def ensure_persistent_codex_dir(
    agent_name: str,
    *,
    username: str = "",
    scope: str = "user",
) -> Path:
    """Create/update the persistent .codex/ dir for a Codex CLI session.

    Same scoping as ensure_persistent_claude_dir but writes Codex-format
    hooks.json instead of Claude-format settings.json.

    Returns the host path to the .codex/ directory.
    """
    agent_dir = app_config.get_agent_dir(agent_name)

    if username and scope == "user":
        codex_dir = agent_dir / "users" / username / ".codex"
    else:
        codex_dir = agent_dir / "workspace" / ".codex"

    codex_dir.mkdir(parents=True, exist_ok=True)

    # Write hooks.json (Codex hook format) — always sandbox-internal paths
    if username and scope == "user":
        sandbox_codex_dir = f"/users/{username}/.codex"
    else:
        sandbox_codex_dir = "/workspace/.codex"
    hooks = _build_codex_hooks(sandbox_codex_dir)

    (codex_dir / "hooks.json").write_text(
        json.dumps(hooks, indent=2) + "\n"
    )

    # Copy hook scripts into .codex/ dir (LF-normalized + executable — see _copy_hook_lf).
    for script_name in ("permission_gate.py", "tool_result_forwarder.py"):
        src = _HOOKS_DIR / script_name
        dst = codex_dir / script_name
        if src.exists():
            _copy_hook_lf(src, dst)

    # Copy the stdio interceptor alongside the hooks (see the claude twin) —
    # reachable inside the bwrap sandbox for the credential-broker fetch.
    # Unconditional so a missing source raises instead of silently skipping.
    _copy_hook_lf(_INTERCEPTOR_SRC, codex_dir / _INTERCEPTOR_SRC.name)

    # Reconcile the platform-managed on-demand skills dir ($CODEX_HOME/skills;
    # Codex's vendored .system builtins are never touched — dot-prefixed).
    # Fail-soft; see skills_materializer for the full protocol.
    from core.sandbox.skills_materializer import materialize_skills_for_sandbox
    materialize_skills_for_sandbox(agent_name, codex_dir)

    os.chmod(codex_dir, 0o700)

    logger.debug(
        f"Prepared .codex/ dir: {codex_dir} "
        f"(agent={agent_name}, user={username or '(none)'})"
    )
    return codex_dir


def ensure_persistent_agent_dir(
    agent_name: str,
    *,
    execution_path: str,
    username: str = "",
    scope: str = "user",
) -> Path:
    """The persistent CLI config dir for a session, by execution layer:
    ``.codex/`` for Codex, ``.claude/`` for Claude CLI (and the harmless default
    for Direct LLM, which has no CLI config).

    **Single source of truth** for this branch so the four session-config builders
    (``config_builder`` / ``task_config_builder`` / ``meeting_orchestrator`` /
    phone) can't drift. The Codex layer reads ``config.sandbox_host_claude_dir`` AS
    its ``CODEX_HOME``; a Codex session whose config landed in ``.claude`` ran
    against a missing ``.codex`` config and hung / crashed at init (an
    interactive-task bug — a builder that forgot the codex branch).
    """
    if execution_path == "codex-cli":
        return ensure_persistent_codex_dir(agent_name, username=username, scope=scope)
    return ensure_persistent_claude_dir(agent_name, username=username, scope=scope)


def _build_codex_hooks(config_dir: str) -> dict:
    """Build the hooks.json content for the Codex CLI hook system.

    Schema (Codex hooks, per the OpenAI Codex docs):
        {"hooks": {"<Event>": [{"matcher": <regex>,
                                "hooks": [{"type": "command",
                                           "command": <cmd>, "timeout": <s>}]}]}}
    Codex passes the SAME PreToolUse stdin shape as Claude (``tool_name`` +
    ``tool_input``) and accepts the SAME ``hookSpecificOutput`` deny output, so
    the provider-agnostic ``permission_gate.py`` / ``tool_result_forwarder.py``
    scripts run unchanged — one ``decide_tool_permission`` authority for every
    surface. Effective only for INTERACTIVE Codex sessions, which set
    ``[features] hooks = true`` + spawn with ``--dangerously-bypass-hook-trust``;
    the app-server leaves the feature off and gates via its JSON-RPC approval
    bridge (enabling both would double-gate). Empty matcher = all tools.
    """
    gate = f"{config_dir}/permission_gate.py"
    forwarder = f"{config_dir}/tool_result_forwarder.py"

    return {
        "hooks": {
            "PreToolUse": [{
                "matcher": "",
                "hooks": [{"type": "command", "command": f"python3 {gate}", "timeout": 604800}],
            }],
            "PostToolUse": [{
                "matcher": "",
                "hooks": [{"type": "command", "command": f"python3 {forwarder}", "timeout": 10}],
            }],
        },
    }


def prepare_mcp_config_for_sandbox(
    host_mcp_config_path: str | Path,
    host_config_dir: str | Path,
    sandbox_config_dir: str = "",
    *,
    session_id: str = "",
    secret_bundles: dict | None = None,
) -> str:
    """Copy MCP config into the session config dir for sandboxed sessions.

    Copies the MCP config file into the config dir (.claude/ or .codex/)
    and returns the sandbox-internal path. The original file in
    proxy/sessions/ is NOT mounted in the sandbox.

    Also rewrites file paths inside the config (e.g. instance config files
    referenced by --config-file args) to sandbox-internal paths, copying
    those files into the config dir too.

    When ``session_id`` + ``secret_bundles`` are given (local CLI broker path),
    each stdio MCP that has a secret bundle gets a per-(session, mcp) capability
    token injected into THIS per-session copy, then its command is wrapped with
    the stdio interceptor so it fetches its secrets at spawn.

    Args:
        host_config_dir: Host path to .claude/ or .codex/ dir.
        sandbox_config_dir: Sandbox-internal config dir path
            (e.g. /users/alice/.claude or /workspace/.codex).
        session_id: Session id for broker token minting (empty → no broker).
        secret_bundles: ``{mcp_name: SecretBundle}`` for this session — only
            these MCPs get a fetch token + interceptor wrap.
    """
    if not host_mcp_config_path:
        return ""

    src = Path(host_mcp_config_path)
    if not src.exists():
        return str(src)

    # Read config, rewrite any referenced file paths, copy referenced files
    import json as _json
    try:
        config_data = _json.loads(src.read_text())
        sessions_dir = str(app_config.SESSIONS_DIR)
        mcp_servers = config_data.get("mcpServers", {})
        for srv in mcp_servers.values():
            args = srv.get("args", [])
            for i, arg in enumerate(args):
                if isinstance(arg, str) and arg.startswith(sessions_dir):
                    # This arg is a host path to a file in sessions/ — copy it
                    ref_path = Path(arg)
                    if ref_path.exists():
                        ref_dst = Path(host_config_dir) / ref_path.name
                        shutil.copy2(ref_path, ref_dst)
                        args[i] = f"{sandbox_config_dir}/{ref_path.name}"

        # Credential broker: inject the per-(session, mcp) capability
        # token into each stdio MCP that has a secret bundle, then wrap its
        # command with the stdio interceptor so it fetches its secrets at spawn.
        # The token lands in THIS per-session copy only — never the shared
        # sessions/ build file (reused across concurrent sessions).
        if session_id and secret_bundles:
            from core.credentials import mcp_broker
            from core.sandbox.interceptor_wrap import wrap_servers_json
            bundle_keys = set(secret_bundles)
            for name, srv in mcp_servers.items():
                if name not in bundle_keys or not isinstance(srv, dict):
                    continue
                if "command" in srv:
                    env = srv.get("env") or {}
                    env["OTO_MCP_FETCH_TOKEN"] = mcp_broker.mint_token(session_id, name)
                    srv["env"] = env
                else:
                    # Proxy-terminable HTTP MCP (github/m365). The shared
                    # build file ships a sentinel bearer; on the TRUSTED proxy
                    # host, swap in the REAL token from the bundle. Local has no
                    # tunnel hop to swap at, so the bearer lives inline in THIS
                    # per-session sandbox copy. The agent CAN read this file
                    # (a same-uid native/Codex tool isn't bound by the hook), but
                    # it is the session principal's OWN token: a user-scope
                    # session carries the user's own subscription token (already
                    # theirs); admin/agent-scope tokens never reach a user-paired
                    # machine, and admin machines are fully trusted. So this is a
                    # same-trust-domain residual, not a cross-principal leak.
                    # Never the shared sessions/ file.
                    # HTTP MCPs with no bundle bearer (vendor, file-tools) are
                    # left untouched.
                    bearer = getattr(secret_bundles.get(name), "http_bearer", None)
                    if bearer:
                        headers = srv.get("headers") or {}
                        headers["Authorization"] = f"Bearer {bearer}"
                        srv["headers"] = headers
            wrap_servers_json(
                config_data, interpreter="python3",
                interceptor_path=f"{sandbox_config_dir}/{_INTERCEPTOR_SRC.name}",
            )

        # Write rewritten config
        dst = Path(host_config_dir) / src.name
        dst.write_text(_json.dumps(config_data, indent=2))
    except Exception:
        # Fallback: simple copy without rewriting
        dst = Path(host_config_dir) / src.name
        shutil.copy2(src, dst)

    # Return sandbox-internal path
    return f"{sandbox_config_dir}/{src.name}"


def materialize_ssh_keys_for_sandbox(
    agent_name: str, host_config_dir: str | Path,
) -> bool:
    """Provision this agent's authorized SSH keys into ``<config_dir>/ssh``.

    ssh-hosts is a context-only MCP — agents run plain ``ssh`` from bash, so
    the keys must exist inside the sandbox. The master copies live in the
    MCP's ``keys/`` dir, which is NEVER sandbox-mounted (the sandbox binds
    only assigned stdio MCP dirs, and ssh-hosts has no server); each session
    instead gets ONLY the keys referenced by the agent's authorizing
    instances, copied 0600 into the session config dir (already private to
    this agent+user and bind-mounted). The dir is wiped and rebuilt every
    session start, so a de-authorized or deleted key disappears on the next
    session.

    Returns True when at least one key landed — the caller then exports
    ``OTO_SSH_KEY_DIR=<sandbox config dir>/ssh`` so the prompt's ready-to-run
    ``ssh -i "$OTO_SSH_KEY_DIR/<key>"`` lines resolve. (Host keys are NOT
    pre-seeded — hosts are often reachable only from the machine the session
    runs on, so the prompt lines carry ``StrictHostKeyChecking=accept-new``
    instead; see ``dynamic_context._ssh_hosts_context``.)
    """
    dst = Path(host_config_dir) / "ssh"
    shutil.rmtree(dst, ignore_errors=True)

    copied = 0
    for key, src in sorted(collect_authorized_ssh_keys(agent_name).items()):
        if copied == 0:
            dst.mkdir(parents=True, exist_ok=True)
            os.chmod(dst, 0o700)
        shutil.copy2(src, dst / key)
        os.chmod(dst / key, 0o600)
        copied += 1
    return copied > 0


def collect_authorized_ssh_keys(agent_name: str) -> dict[str, Path]:
    """The SSH key files this agent's ssh-hosts authorization grants.

    Returns ``{key_name: absolute source Path}`` for the keys referenced by
    the agent's authorizing ssh-hosts instances — empty when the MCP isn't
    installed / isn't enabled for the agent (visible + manager-enabled +
    platform-enabled, the same gate every runtime surface uses) / no instance
    references a key. Key names are restricted to sanitized basenames so a
    poisoned instance row can never become a file-read primitive.

    Shared by the local sandbox materializer above and the remote
    session-file provisioning in ``core/remote/remote_execution``.
    """
    from services.mcp import mcp_registry
    from storage import mcp_store

    manifest = mcp_registry.get_manifest("ssh-hosts")
    if manifest is None:
        return {}
    if not any(m.name == "ssh-hosts" for m in mcp_registry.get_agent_mcps(agent_name)):
        return {}

    keys_dir = manifest.mcp_dir / "keys"
    out: dict[str, Path] = {}
    for inst in mcp_store.get_mcp_instances_for_agent("ssh-hosts", agent_name):
        key = ((inst.get("field_values") or {}).get("key_name") or "").strip()
        if not key or os.path.basename(key) != key:
            continue
        src = keys_dir / key
        if src.is_file():
            out[key] = src
    return out
