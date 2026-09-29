"""Per-session config-directory setup for agent sandboxes — the shared spine.

Where a session's config dir lives under its scope root and what every
engine installs into it (the hook scripts, the stdio interceptor), the one
funnel the session-config builders call (``ensure_persistent_agent_dir`` →
``ExecutionLayer.prepare_config_dir``), the sandbox-side MCP config copy
and the SSH key materialisation. The engine-specific bodies — Claude's
``settings.json`` and its built-in deny list, Codex's ``hooks.json`` — are
the engines' own (``core/layers/cli/config_dir.py``,
``core/layers/codex/config_dir.py``). ``sandbox.py`` re-exports the funnel
and the MCP config copy for its historical importers.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

import config as app_config
from core import layout

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


def write_no_follow(path: Path, data: bytes, mode: int = 0o644) -> None:
    """Write ``data`` to ``path`` refusing to follow a symlink at the leaf.

    The ``.claude``/``.codex`` trees sit inside agent-WRITABLE binds, so an
    agent can replace ``settings.json`` (or a hook script) with a symlink
    between sessions — a plain ``write_text`` would then write host-side at
    the symlink's target as the proxy uid. An existing symlink is unlinked
    and the file recreated; ``O_NOFOLLOW`` closes the race. Mode is set via
    ``fchmod`` on the open fd (no separate follow-prone chmod on the path).
    """
    try:
        if path.is_symlink():
            path.unlink()
    except FileNotFoundError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW,
                 0o644)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            fd = -1
            f.write(data)
    finally:
        if fd != -1:
            os.close(fd)


def _verified_session_dir(agent_name: str, *parts: str) -> Path:
    """Build ``agents/<agent>/parts...`` refusing symlinked components.

    Same invariant as ``sandbox._verified_literal_path``: the dir chain lives
    under agent-writable binds, so verify realpath == literal BEFORE mkdir
    (a follow-prone ``mkdir -p`` would create dirs at a planted symlink's
    target) and again after. Raises on tampering — a session must fail
    loudly rather than write its config through a redirected path.
    """
    root_real = Path(os.path.realpath(app_config.get_agent_dir(agent_name)))
    expected = root_real.joinpath(*parts)
    if os.path.realpath(expected) != str(expected):
        raise RuntimeError(
            f"Refusing session config dir {expected}: a path component is a "
            f"symlink (possible tampering)"
        )
    expected.mkdir(parents=True, exist_ok=True)
    if os.path.realpath(expected) != str(expected):
        raise RuntimeError(
            f"Refusing session config dir {expected}: path changed "
            f"underneath the build (possible tampering)"
        )
    return expected


def _copy_hook_lf(src: Path, dst: Path) -> None:
    """Copy a hook/interceptor script into a sandbox, normalizing to LF and
    forcing the executable bit. A stray CR in the shebang
    (``#!/usr/bin/env python3\\r``) makes the kernel look for an interpreter
    literally named ``python3\\r`` → the hook fails to start
    (``/usr/bin/env: 'python3\\r': No such file or directory``) and silently
    bypasses enforcement. Normalize defensively so an editor/git CRLF can never
    break hook execution inside the sandbox (vs ``shutil.copy2``, which copies
    bytes + perms verbatim). Writes via ``write_no_follow`` — the dst lives
    in an agent-writable tree, so a planted symlink must not be followed."""
    write_no_follow(dst, src.read_bytes().replace(b"\r\n", b"\n"), mode=0o755)


# ---------------------------------------------------------------------------
# The shared spine: where a session's config dir lives, and what every
# engine installs into it. The engine-specific bodies — Claude's
# settings.json and its built-in deny list, Codex's hooks.json — live in the
# engine packages (core/layers/cli/config_dir.py, core/layers/codex/
# config_dir.py) behind ExecutionLayer.prepare_config_dir.
# ---------------------------------------------------------------------------

def scope_config_dir(
    agent_name: str, name: str, *, username: str = "", scope: str = "user",
    external_home=None,
) -> tuple[Path, str]:
    """``(host dir, sandbox-internal dir)`` of a session's config dir
    ``name`` (``.claude`` / ``.codex``) under the scope's root — created,
    verified against planted symlinks:

    - a user session: ``agents/<agent>/users/<username>/<name>`` ↔
      ``/users/<username>/<name>``
    - an agent-scoped session (a task, a phone call without a user):
      ``agents/<agent>/workspace/<name>`` ↔ ``/workspace/<name>``
    - an external caller with a private tree (``external_home``):
      ``<external_home>/<name>`` ↔ ``<SANDBOX_HOME>/<name>``
    """
    if external_home:
        from core.session.external_identity import SANDBOX_HOME
        return _verified_external_dir(agent_name, external_home, name), f"{SANDBOX_HOME}/{name}"
    if username and scope == "user":
        return (_verified_session_dir(agent_name, layout.USERS, username, name),
                f"{layout.virtual_user_root(username)}/{name}")
    return _verified_session_dir(agent_name, layout.WORKSPACE, name), f"{layout.V_WORKSPACE}/{name}"


def install_hook_scripts(config_dir: Path) -> None:
    """Copy the four hook scripts and the stdio interceptor into a session
    config dir (LF-normalised, executable — a CRLF shebang silently breaks
    hook execution). The interceptor is copied unconditionally: a missing
    source must raise, not silently disable the wrap spawn-time config still
    points at."""
    for script_name in HOOK_SCRIPTS:
        src = _HOOKS_DIR / script_name
        if src.exists():
            _copy_hook_lf(src, config_dir / script_name)
    _copy_hook_lf(_INTERCEPTOR_SRC, config_dir / _INTERCEPTOR_SRC.name)


#: Every hook script a session config dir carries, for both engines
#: (``proxy/hooks/``; the satellite receives the same set in its start payload).
HOOK_SCRIPTS: tuple[str, ...] = (
    "permission_gate.py", "tool_result_forwarder.py",
    "subagent_tracker.py", "stop_tracker.py",
)

#: The permission gate's and the Stop hook's transport ceiling: a dashboard
#: prompt and a turn-end verdict are long polls the proxy bounds itself.
LONG_HOOK_TIMEOUT_S = 604800
STOP_HOOK_TIMEOUT_S = LONG_HOOK_TIMEOUT_S


def _verified_external_dir(agent_name: str, external_home, sub: str) -> Path:
    """``<external_home>/<sub>`` for an external caller's tree, refusing
    symlinked components (the same invariant as ``_verified_session_dir``)
    and any home that is not a plain directory under this agent's tree."""
    root_real = Path(os.path.realpath(app_config.get_agent_dir(agent_name)))
    home = Path(external_home)
    if os.path.realpath(home) != str(home) or not home.is_relative_to(root_real):
        raise RuntimeError(
            f"Refusing external session dir under {home}: not a plain "
            "directory under the agent tree (possible tampering)"
        )
    return _verified_session_dir(agent_name, *home.relative_to(root_real).parts, sub)

def ensure_persistent_agent_dir(
    agent_name: str,
    *,
    execution_path: str,
    username: str = "",
    scope: str = "user",
    external_home=None,
    no_shell: bool = False,
    read_only: bool = False,
) -> Path:
    """The persistent CLI config dir for a session — the ENGINE's
    (``ExecutionLayer.prepare_config_dir``): ``.codex/`` for Codex,
    ``.claude/`` for Claude CLI and for Direct LLM (which has no CLI config
    but keeps its plans dir and MCP config home in that tree).

    **The one entry point** for the session-config builders
    (``config_builder`` / ``task_config_builder`` / ``meeting_orchestrator`` /
    phone / the scheduler's one-shot resume) so they can't drift. The Codex
    layer reads ``config.sandbox_host_claude_dir`` AS its ``CODEX_HOME``; a
    Codex session whose config landed in ``.claude`` ran against a missing
    ``.codex`` config and hung / crashed at init (an interactive-task bug —
    a builder that forgot the codex branch, before this funnel existed).
    An unregistered ``execution_path`` raises ``UnknownExecutionPath``, as
    every builder already does one step earlier. The registry is reached
    function-locally: the layers import this package.
    """
    from core.session.session_manager import get_layer_by_path
    return get_layer_by_path(execution_path).prepare_config_dir(
        agent_name, username=username, scope=scope, external_home=external_home,
        no_shell=no_shell, read_only=read_only,
    )

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
                        write_no_follow(ref_dst, ref_path.read_bytes())
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

        # Write rewritten config. No-follow: this per-session copy carries
        # broker tokens / inline bearers and the destination dir is
        # agent-writable — a planted symlink must never redirect it.
        dst = Path(host_config_dir) / src.name
        write_no_follow(dst, _json.dumps(config_data, indent=2).encode())
    except Exception:
        # Fallback: simple copy without rewriting (same no-follow rule —
        # falling back to a follow-prone copy would void the guard above).
        dst = Path(host_config_dir) / src.name
        write_no_follow(dst, src.read_bytes())

    # Return sandbox-internal path
    return f"{sandbox_config_dir}/{src.name}"


class AgentStateRefused(RuntimeError):
    """A session that would run from the agent's own CLI state (the agent
    scope's ``workspace/.claude`` / ``.codex``: the hooks, settings, MCP
    config and login every task, phone call and Shared-only chat of the
    agent runs with) for a person below the editor tier. Whoever runs from
    that dir can rewrite what the agent's other sessions execute, so only
    the tiers that already act as the agent may. The message is the
    person's to read."""


def refuse_agent_state_below_editor(scope: str, role: str, *, external: bool = False) -> None:
    """Raise :class:`AgentStateRefused` when a session of ``scope`` (its
    MOUNT scope) and ``role`` would run from the agent's own CLI state below
    the editor tier: a Shared-only chat of a viewer or a contributor, or
    their own task or meeting on a Shared-only agent. An external caller is
    not a person on the agent (a no-shell session the gate floors), and a
    personal session runs from its own tree."""
    from auth import roles
    from core.session.visibility import SCOPE_AGENT
    if scope != SCOPE_AGENT or external or roles.can_edit(role):
        return
    raise AgentStateRefused(
        "This agent is set to Shared only, so its chats and tasks run as the "
        "agent itself, which takes the editor role or above (this one would "
        f"run as {role or 'no role'}). Ask a manager of the agent for the "
        "editor role, or to turn on personal chats."
    )


def refuse_session_on_agent_state(ctx) -> None:
    """:func:`refuse_agent_state_below_editor` for a built session, from its
    SecurityContext: the start-time floor every engine checks before it runs
    a CLI from its config dir (a builder the config-time check missed, a
    context rebuilt for a resume). The session runs from the agent's state
    when it mounts no person (``mount_username``, the sandbox's own rule);
    no context at all is refused as below the tier."""
    from core.session.external_identity import is_external_ctx
    from core.session.visibility import SCOPE_AGENT, SCOPE_USER
    if ctx is None:
        refuse_agent_state_below_editor(SCOPE_AGENT, "")
        return
    scope = SCOPE_USER if getattr(ctx, "mount_username", "") else SCOPE_AGENT
    refuse_agent_state_below_editor(
        scope, getattr(ctx, "role", "") or "", external=is_external_ctx(ctx),
    )


def session_takes_ssh_keys(ctx) -> bool:
    """Whether a session is handed the agent's SSH keys, locally or on a
    machine: the editor tier of its SecurityContext (the keys are the
    agent's own credentials, so only the tiers that act as the agent),
    never an external caller's (ssh-hosts is not attached there, and key
    material must not land in a tree a caller's session reaches), never a
    check's judge (it reads; its MCPs are the check's list), and never a
    session with no context or no role. The one predicate the Claude and
    Codex layers and the satellite session-file broker share; the role is
    read off the context alone, never inferred from a token or a name."""
    from auth import roles
    from core.session.external_identity import is_external_ctx
    if ctx is None or is_external_ctx(ctx) or getattr(ctx, "read_only", False) is True:
        return False
    role = getattr(ctx, "role", "")
    return isinstance(role, str) and roles.can_edit(role)


def clear_ssh_keys_for_sandbox(host_config_dir: str | Path) -> None:
    """Remove ``<config_dir>/ssh``: the session does not take the agent's
    keys, so nothing an earlier, more entitled session of the same person
    left in that dir may survive into this one (the materializer rebuilds
    the dir only when the keys are due)."""
    dst = Path(host_config_dir) / "ssh"
    if dst.is_symlink():
        dst.unlink()
        return
    shutil.rmtree(dst, ignore_errors=True)


def provision_ssh_keys_for_sandbox(
    ctx, agent_name: str, host_config_dir: str | Path, sandbox_config_dir: str,
) -> str:
    """The one SSH-key rule of the local layers, at spawn: a session that
    takes the keys (:func:`session_takes_ssh_keys`) gets them materialised
    and the ``OTO_SSH_KEY_DIR`` value the env carries (``""`` when the agent
    authorises none); a person below the editor tier gets the dir cleared,
    so nothing an earlier, entitled session of theirs left survives into
    this one; a judge and an external caller take no keys but leave the dir
    alone, because they run from a config dir another session may hold the
    keys in (the judged person's, the agent's own)."""
    if session_takes_ssh_keys(ctx):
        if materialize_ssh_keys_for_sandbox(agent_name, host_config_dir):
            return f"{sandbox_config_dir}/ssh"
        return ""
    from auth import roles
    from core.session.external_identity import is_external_ctx
    if ctx is None or is_external_ctx(ctx) or getattr(ctx, "read_only", False) is True:
        return ""
    if isinstance(getattr(ctx, "role", None), str) and not roles.can_edit(ctx.role):
        clear_ssh_keys_for_sandbox(host_config_dir)
    return ""


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
    # rmtree refuses (ignores) a symlink — an agent-planted ``ssh`` link
    # would survive it, pass mkdir(exist_ok=True) via its target, and the
    # key material below would land host-side at the target. Remove it.
    if dst.is_symlink():
        dst.unlink()

    copied = 0
    for key, src in sorted(collect_authorized_ssh_keys(agent_name).items()):
        if copied == 0:
            dst.mkdir(parents=True, exist_ok=True)
            os.chmod(dst, 0o700)
        write_no_follow(dst / key, src.read_bytes(), mode=0o600)
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
    session-file provisioning in ``core/remote/remote_session_start``.
    """
    from services.mcp import mcp_registry
    from storage.mcp import mcp_store

    manifest = mcp_registry.get_manifest("ssh-hosts")
    if manifest is None:
        return {}
    if not any(m.name == "ssh-hosts" for m in mcp_registry.get_agent_mcps(agent_name)):
        return {}

    keys_dir = manifest.mcp_dir / "keys"
    out: dict[str, Path] = {}
    for inst in mcp_store.get_mcp_instances_for_agent("ssh-hosts", agent_name):
        key = ((inst.get("field_values") or {}).get("key_name") or "").strip()
        if not key or os.path.basename(key) != key or key in (".", ".."):
            continue
        src = keys_dir / key
        if src.is_file():
            out[key] = src
    return out
