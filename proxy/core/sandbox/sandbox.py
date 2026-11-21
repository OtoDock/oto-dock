"""What a session's sandbox is: its mounts, its masks and its network routes.

``SandboxBuilder`` resolves a ``SandboxConfig`` into the description of a
session's sandbox (``describe``), which names nothing of this host. The
machine that runs the session renders the description into the launcher
and bubblewrap command (``core/sandbox/sandbox_render.py``). The platform
itself starts no sandbox.

The engines' config-dir builders (core/layers/<x>/config_dir.py) create and populate the persistent
.claude/ directory for a session (hooks, settings.json).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import psycopg

import config as app_config
from auth import roles
from core import layout
from core.sandbox import sandbox_render as _render

if TYPE_CHECKING:
    from services.mcp.mcp_manifest_types import McpManifest

logger = logging.getLogger("otodock.sandbox")


#: Where an external caller's own tree appears inside the session
#: (``core/session/external_identity.py`` owns the on-disk layout).
EXTERNAL_SANDBOX_HOME = "/caller"


def empty_mount_dir() -> Path:
    """A platform-owned, always-empty directory used as an RO bind SOURCE to
    mask a subtree out of a session (the shared memory on external
    sessions). Lives under the sessions dir, never under an agent tree, so
    no session can populate it."""
    p = app_config.SESSIONS_DIR / ".empty"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _verified_literal_path(root_real: Path, *parts: str) -> Path | None:
    """Resolve ``root_real/parts...`` and demand it IS that literal path.

    ``root_real`` must already be fully resolved (``os.path.realpath``).
    Returns the literal path when no component below the root is a symlink
    (``realpath == expected``), else ``None``. Required for every bind
    source / host-side dir operation under an agent-WRITABLE tree: an agent
    holding the parent RW in one session can plant a symlink there, and a
    follow at build/bind time moves the operation to the symlink's target
    on the HOST (the file-transfer B4 class — a planted
    ``users/<u>/.claude → /`` would otherwise become an RW bind of the host
    root in that person's next session).
    """
    expected = root_real.joinpath(*parts)
    if os.path.realpath(expected) != str(expected):
        return None
    return expected


def _mkdirs_held(root_real: Path, *parts: str) -> Path:
    """Create ``root_real/parts...`` with no link followed below the root
    (``safe_fs.mkdirs_beneath``) and return the literal path. A component
    swapped for a link after the caller's ``_verified_literal_path`` check
    refuses the build here instead of creating directories at the link's
    target on the host. ``root_real`` is the agent's resolved folder, which
    no sandbox can rename (it is never bound)."""
    from services.infra import safe_fs
    root_real.mkdir(parents=True, exist_ok=True)
    try:
        safe_fs.mkdirs_beneath(root_real, "/".join(parts))
    except safe_fs.SafeFsError as exc:
        raise RuntimeError(
            f"Refusing sandbox build: {'/'.join(parts)} changed underneath the "
            "build (possible tampering)"
        ) from exc
    return root_real.joinpath(*parts)


# ---------------------------------------------------------------------------
# Network namespace isolation (always on)
# ---------------------------------------------------------------------------
#
# Every sandboxed session runs inside a pasta-managed network namespace
# (RFC1918 + the host's own subnet + cloud-metadata blackholed), with egress
# carved ONLY to the session's legitimate destinations (proxy hook port +
# configured MCP / docker-MCP / local-LLM targets — see resolve_sandbox_egress).
# There is no un-isolated sandbox mode: genuinely un-isolated execution is a
# paired computer whose owner chose it. Un-resolvable egress fails CLOSED
# (the render refuses a description with no route) — never a silent
# fall-back to host networking.


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SandboxMount:
    """A conditional mount from an MCP manifest's sandbox config."""
    host: str       # resolved host path
    sandbox: str    # mount point inside sandbox
    mode: str       # "ro" or "rw"


def mcp_sandbox_mounts(manifests: Iterable[McpManifest]) -> list[SandboxMount]:
    """The mounts the given MCP manifests declare, each host's ``${mcp_dir}``
    template resolved. The host is allowlisted to the agent / mcps tree in
    the sandbox builder, so a platform-root template is not offered here."""
    mounts: list[SandboxMount] = []
    for manifest in manifests:
        for m in getattr(manifest, "sandbox_mounts", []):
            host = m.host.replace("${mcp_dir}", str(manifest.mcp_dir))
            mounts.append(SandboxMount(host=host, sandbox=m.sandbox, mode=m.mode))
    return mounts


@dataclass(frozen=True)
class Mount:
    """One decided entry of the role mount table: the host directory ``host``
    appears at ``sandbox`` inside the session, read-write when ``rw``.

    ``SandboxBuilder.workspace_mount_table`` is the single source of truth for
    what a session can read and write: the render turns the list into
    bwrap ``--bind`` / ``--ro-bind`` args for CLI + MCP processes and for the
    Direct-LLM file tools' program, which runs inside that sandbox. The
    platform's half of those tools (core/layers/direct/files.py) opens
    nothing and reads the SAME list as text for three things: to word an
    early refusal, to judge the place where a link lands (``files.landing``),
    and to name the file that the bookkeeping records and the delete owner
    removes (``files.record_place``). Order
    matters: bwrap gives a later bind precedence at the same or a deeper
    destination (the RO root + RW subdir stacking below relies on it), so a
    reader of the list must pick the longest matching ``sandbox`` prefix and,
    on a tie, the later entry.
    """
    host: str       # real directory on the proxy host
    sandbox: str    # mount point inside the session
    rw: bool


@dataclass(frozen=True)
class SandboxConfig:
    """Immutable description of a sandbox for one session."""
    role: str                                # viewer / manager / admin
    username: str                            # "" for agent-scoped tasks
    agent_name: str
    is_admin_agent: bool
    host_agents_dir: Path                    # config.AGENTS_DIR resolved
    host_mcps_dir: Path                      # absolute path to mcps/
    host_claude_dir: Path                    # persistent .claude/ on host
    # Visibility-modes decouple. ``username`` above is the MOUNT username ("" for
    # any agent-scope mount, incl. a Shared-only HUMAN chat), so it alone no
    # longer tells the mount builder whether the human is an owner or which
    # shared dirs the mode offers:
    #   config_visible — owner-tier human → mount /config (RW) + knowledge RW.
    #                    False for service sessions (the admin-only-task guard).
    #                    ``None`` = derive historically (owner role + real mount
    #                    user) so direct construction / legacy callers are
    #                    unchanged; the resolver passes a concrete bool.
    #   mount_shared   — does this agent's mode include the shared /workspace +
    #                    /knowledge? False only for Personal-only.
    config_visible: bool | None = None
    mount_shared: bool = True
    # Manager-provenance knowledge writes (agent-scope task fires only):
    # mounts /knowledge RW on the agent-scope branch — WITHOUT /config,
    # which stays human-manager-only. Computed by resolve_task_identity
    # from the dynamic task's creator; False everywhere else.
    knowledge_rw: bool = False
    # The judge profile (CHECKS.md "The judge profile"): every row of the
    # table this identity would get, flipped read-only after it is built —
    # the CLI's own state dirs (.claude / .codex) excepted,
    # since the CLI writes its session there. /config never mounts for a
    # judge (config_visible is False). Kernel-enforced locally; the gate's
    # ``judge`` mode is the same rule on a machine.
    read_only: bool = False
    # External routes (core/session/external_identity.py): the session's
    # caller is not a platform user. ``external`` masks the shared memory
    # (knowledge/memory) out of the RO /knowledge bind; ``external_home``
    # (host path; "" when the agent's mode has no personal scope or the
    # target is remote) mounts the caller's own tree at /caller — RO root
    # with workspace/, context/ and the CLI state dirs RW, like /users/{u}.
    # Both ride the agent-scope branch (``username == ""``).
    external: bool = False
    external_home: str = ""
    # An outside session's picks (services/mcp/outside_picks.py): whether the
    # person's own files are read-write, and the knowledge folders bound
    # read-only (a folder name, or ``shared/<source>[/<subdir>]`` for an
    # attached library). No shared workspace and no knowledge root ever.
    outside_files_write: bool = False
    outside_knowledge: tuple[str, ...] = ()
    # The wrap-up after a conversation (services/phone/wrapup.py): ``None``
    # for every other session. The shared workspace and the knowledge read
    # only, the person's tree (``wrapup_home``, "" for none) at /caller with
    # workspace/ and context/ writable and its memory read only. Agent scope.
    wrapup_home: str | None = None
    # A session that runs from its own scratch engine home instead of its
    # scope's (the wrap-up, the agent's own phone line; ``own_engine_home``):
    # that home's .claude and .codex at /caller/.claude and /caller/.codex, its
    # config dir there, and the agent scope's own engine state masked. The
    # line also has the agent's shared memory masked.
    engine_home: str = ""
    # The session's own home (``core/sandbox/session_home``), its host path,
    # bound back in writable over the masked ``own`` folder (``describe``);
    # "" for every sandbox that is not a session running from one.
    session_home: str = ""
    # Attached knowledge libraries: ``(source_slug, subdir, writable)`` per
    # row of knowledge_library_attachments (``subdir == ''`` = whole-folder
    # share). Mirrors are real dirs inside THIS agent's knowledge tree
    # (``knowledge/shared/<source>/<subdir>/``), so they ride the parent
    # /knowledge bind wherever it exists; the mount builder adds a nested
    # ``--ro-bind`` of each read-only library's subtree when the parent is
    # RW (kernel-level RO), and mounts library subtrees individually
    # (always RO) on Personal-only agents with no parent /knowledge at all.
    knowledge_libraries: list[tuple[str, str, bool]] = field(default_factory=list)
    mcp_sandbox_mounts: list[SandboxMount] = field(default_factory=list)
    extra_ro_binds: list[str] = field(default_factory=list)  # additional RO paths to mount
    # Per-MCP dirs to identity-bind RO (this session's assigned stdio MCPs +
    # mcps/.uv-python). The whole mcps/ tree is NEVER mounted — an agent must
    # not be able to read the code/config/data of MCPs it isn't assigned
    # (e.g. another MCP's data_dirs). Empty = no MCP dirs (fail closed).
    mcp_dir_binds: list[str] = field(default_factory=list)
    # Sandbox egress allow-set, resolved by
    # services/mcp_registry.resolve_sandbox_egress():
    #   net_forwards   — loopback ports pasta -T-splices (proxy hook port + T1
    #                    Docker-MCP host-loopback ports). Always ≥ the proxy
    #                    port for a resolved session; EMPTY is a build error and
    #                    fails closed (refuses to launch — never un-isolated).
    #   net_allow_hosts— routable IPs carved back out of the blackholes (T2
    #                    Docker-MCP container IPs + enabled homelab MCP targets).
    net_forwards: list[str] = field(default_factory=list)
    net_allow_hosts: list[str] = field(default_factory=list)
    # App servers (services/apps/app_sandbox.py, APPS.md): an explicit mount
    # table that REPLACES the role table above (the release copy read-only
    # at /app, the app's data directory read-write at /app/data, the Bun
    # binary), a cwd of /app, one inbound splice "hostport:port" so the
    # proxy can reach the server, and a closed public internet. None / ""
    # / False for every agent session.
    app_mounts: list[Mount] | None = None
    app_cwd: str = ""
    # An app button's tools (services/apps/headless_exec.py): every engine
    # state the table would bind is masked, at the app's own role too.
    app_press: bool = False
    net_inbound: str = ""
    net_egress_deny: bool = False


# Sandbox-internal destinations an MCP manifest mount must NEVER target.
# Conditional manifest mounts run LAST in the render, so a later
# ``--ro-bind`` would shadow whatever already sits at the destination. Without
# this guard a malicious community manifest could overlay its own file onto the
# permission-gate hook (under ``.claude``) to disable tool gating, or shadow
# ``/etc``/``/proc``/the shared ``/config`` + ``/knowledge`` trees. The
# permission hook + per-session config/secrets live under ``.claude``/``.codex``.
_PROTECTED_MOUNT_DEST_ROOTS = (
    layout.V_CONFIG, layout.V_KNOWLEDGE, "/etc", "/proc", "/sys", "/dev", "/run",
    "/tmp", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var", "/root",
    "/boot", "/home",
)
_PROTECTED_MOUNT_DEST_EXACT = frozenset({"/", layout.V_WORKSPACE, layout.V_USERS})


def _is_safe_mcp_mount_dest(dest: str) -> bool:
    """Whether a manifest mount destination is safe to bind inside the sandbox.

    Rejects non-absolute paths, the workspace/users/root mount points
    themselves, anything under a protected system/shared tree, and any path
    that traverses a ``.claude``/``.codex``/``.ssh`` dir (where the permission
    hook + per-session secrets live).
    """
    if not dest or not dest.startswith("/"):
        return False
    norm = os.path.normpath(dest)
    if norm in _PROTECTED_MOUNT_DEST_EXACT:
        return False
    parts = norm.split("/")
    if ".claude" in parts or ".codex" in parts or ".ssh" in parts:
        return False
    return not any(
        norm == root or norm.startswith(root + "/")
        for root in _PROTECTED_MOUNT_DEST_ROOTS
    )


# ---------------------------------------------------------------------------
# SandboxBuilder
# ---------------------------------------------------------------------------

class SandboxBuilder:
    """Resolves a SandboxConfig into the description of a session's sandbox
    (``describe``), which the machine that runs the session renders. Starts
    no process.
    """

    def __init__(self, cfg: SandboxConfig):
        self.cfg = cfg
        self._agent_dir = cfg.host_agents_dir / cfg.agent_name
        # The credential exit's port inside the sandbox (0 for none) and the
        # session's SSH agent socket by its name under the host's socket dir
        # ("" for none): set by the starting layer once the resolve is done
        # (``credential_exit.attach_sandbox``, ``ssh_signer.attach_sandbox``),
        # read by ``describe``.
        self.exit_port = 0
        self.ssh_agent = ""

    def _agent_root_real(self) -> Path:
        """The agent's folder resolved: the one spelling every bind source
        of the role table and the launcher's ``--beneath`` use. The folder
        itself is platform-owned and never bound, so a link there (a folder
        moved to another disk) is the operator's and is followed once here."""
        return Path(os.path.realpath(self._agent_dir))


    def describe(self) -> _render.SandboxDescription:
        """What this session's sandbox is, with nothing of this host in it:
        the role table and the hook binds as roots and relative paths (an
        app server's table keeps its absolute sources, which only the proxy
        renders), the session's tool folders, the manifest mounts the
        builder checked, the working directory and the network routes with
        the proxy port spelled as its marker."""
        root = str(self._agent_root_real())
        table = self.workspace_mount_table()
        binds = tuple(self._bind_of(m, root) for m in table)
        masks, home = self._home_parts(table, root)
        mcps_root = str(self.cfg.host_mcps_dir)
        proxy_port = str(app_config.PORT)
        return _render.SandboxDescription(
            agent=self.cfg.agent_name,
            binds=binds,
            mcp_binds=tuple(self._mcp_rels(mcps_root)),
            mcps_root=mcps_root,
            extra_binds=tuple(self._extra_binds(root)),
            cwd=self.get_cwd(),
            proxy_port=int(app_config.PORT),
            forwards=tuple(_render.PROXY_FORWARD if str(p) == proxy_port else str(p)
                           for p in self.cfg.net_forwards),
            allow_hosts=tuple(str(h) for h in self.cfg.net_allow_hosts),
            hosted=bool(app_config.OTODOCK_CLOUD),
            inbound=self.cfg.net_inbound or "",
            egress_deny=bool(self.cfg.net_egress_deny),
            # The role table's sources all sit in the agent's resolved
            # folder, which the shim holds; an app's table is its own.
            beneath=self.cfg.app_mounts is None,
            tmp_size_mb=int(app_config.SANDBOX_TMP_SIZE_MB or 0),
            exit_port=int(self.exit_port or 0),
            ssh_agent=self.ssh_agent or "",
            home_masks=masks,
            home=home,
        )

    def _home_parts(self, table: list[Mount], root: str
                    ) -> tuple[tuple[_render.Bind, ...], _render.Bind | None]:
        """The sessions' homes the render masks (``sandbox_render.home_args``):
        the ``own`` folder of each engine folder of this agent the table
        shows, wherever it shows it (the session's own scope, the agent
        scope's through a person's ``/workspace``, every person's under an
        admin's ``/users``), and this session's own home. Every sandbox over
        a store masks them (a session, a script, an app button's tools): a
        home holds its session's sign-in, so only the session that runs from
        it names it (``SandboxConfig.session_home``)."""
        from core.sandbox import session_home
        base = Path(root)
        stores = [base / layout.WORKSPACE / n for n in _STATE_DIR_NAMES]
        users = base / layout.USERS
        if users.is_dir() and not users.is_symlink():
            stores += [u / n for u in sorted(users.iterdir())
                       if u.is_dir() and not u.is_symlink() for n in _STATE_DIR_NAMES]
        masks: list[_render.Bind] = []
        for store in stores:
            seen = _seen_at(table, str(store))
            if seen is None or store.is_symlink() or not store.is_dir():
                continue
            masks.append(_render.Bind(_render.ROOT_AGENT, f"{store.relative_to(base)}/{session_home.OWN}",
                                      f"{seen}/{session_home.OWN}", False))
        home = None
        if self.cfg.session_home and session_home.is_home(self.cfg.session_home):
            store = session_home.store_of(self.cfg.session_home)
            seen = _seen_at(table, str(store))
            if seen is not None and Path(self.cfg.session_home).is_relative_to(base):
                home = _render.Bind(_render.ROOT_AGENT, str(Path(self.cfg.session_home).relative_to(base)),
                                    f"{seen}/{session_home.relative(Path(self.cfg.session_home).name)}", True)
        return tuple(masks), home

    @staticmethod
    def _bind_of(m: Mount, root: str) -> _render.Bind:
        """A row of the table as the description spells it: below the
        agent's resolved folder, the empty mask folder, or (an app server's
        table) an absolute source of this host."""
        if m.host == str(empty_mount_dir()):
            return _render.Bind(_render.ROOT_EMPTY, "", m.sandbox, m.rw)
        if m.host == os.devnull:
            return _render.Bind(_render.ROOT_NULL, "", m.sandbox, m.rw)
        if m.host == root:
            return _render.Bind(_render.ROOT_AGENT, "", m.sandbox, m.rw)
        if m.host.startswith(root + "/"):
            return _render.Bind(_render.ROOT_AGENT, m.host[len(root) + 1:], m.sandbox, m.rw)
        return _render.Bind(_render.ROOT_ABS, m.host, m.sandbox, m.rw)

    def _mcp_rels(self, mcps_root: str) -> list[str]:
        """This session's tool folders relative to the tools root: identity
        binds, read-only, at the same path inside (the MCP config names
        them). Only the folders of ``cfg.mcp_dir_binds`` (the assigned stdio
        MCPs and ``mcps/.uv-python``), never the whole tree: an agent must
        not read the code, config or data of a tool it is not assigned. The
        render skips a folder the host lacks. A folder named through a link
        is taken by its place under the tools root, and one outside the root
        is left out: the render refuses a name that climbs out of it."""
        rels: list[str] = []
        for d in self.cfg.mcp_dir_binds:
            real = d if d.startswith(mcps_root + "/") else os.path.realpath(d)
            if real.startswith(mcps_root + "/"):
                rels.append(real[len(mcps_root) + 1:])
            else:
                logger.warning("tool folder %s is outside the tools folder: not bound", d)
        return rels

    def _extra_binds(self, root: str) -> list[_render.Bind]:
        """The mounts an MCP manifest declares (``sandbox.mounts``), each end
        checked against a malicious community manifest: the source must
        resolve inside this agent's own tree or the tools tree (never the
        platform root with ``config.env`` and ``sessions/``, never another
        agent's), and the destination must not overlay the hooks, the
        per-session config, ``/config``, ``/knowledge`` or a system mount
        (``_is_safe_mcp_mount_dest``), since these binds run last. A source
        the host lacks is skipped, here and at the render."""
        mcps_root = Path(self.cfg.host_mcps_dir)
        with contextlib.suppress(OSError, ValueError):
            mcps_root = mcps_root.resolve()
        binds: list[_render.Bind] = []
        for mount in self.cfg.mcp_sandbox_mounts:
            if not os.path.exists(mount.host):
                continue
            host_resolved = Path(mount.host).resolve()
            if host_resolved.is_relative_to(root):
                bind_root, rel = _render.ROOT_AGENT, str(host_resolved.relative_to(root))
            elif host_resolved.is_relative_to(mcps_root):
                bind_root, rel = _render.ROOT_MCPS, str(host_resolved.relative_to(mcps_root))
            else:
                logger.warning(
                    "Refusing MCP sandbox mount of %s (mode=%s): host outside "
                    "the agent / mcps tree", mount.host, mount.mode,
                )
                continue
            if not _is_safe_mcp_mount_dest(mount.sandbox):
                logger.warning(
                    "Refusing MCP sandbox mount to %s (mode=%s): destination "
                    "overlays a protected sandbox path", mount.sandbox, mount.mode,
                )
                continue
            binds.append(_render.Bind(bind_root, rel, mount.sandbox, mount.mode == "rw"))
        return binds

    def get_cwd(self) -> str:
        """Sandbox-internal CWD for this role."""
        username = self.cfg.username

        if self.cfg.app_cwd:
            return self.cfg.app_cwd
        if self.cfg.external_home or self.cfg.wrapup_home is not None:
            # External caller with a private tree — their own home; the
            # wrap-up after a conversation, the person's (or its scratch).
            return EXTERNAL_SANDBOX_HOME
        if not username:
            # Agent-scoped task
            return layout.V_WORKSPACE
        # All user roles (viewer / manager / admin)
        return layout.virtual_user_root(username)

    def get_env_overrides(
        self,
        config_dir_name: str = ".claude",
        config_env_var: str = "CLAUDE_CONFIG_DIR",
    ) -> dict[str, str]:
        """Extra env vars to set for the sandboxed process.

        Args:
            config_dir_name: Name of the config directory (".claude" or ".codex").
            config_env_var: Env var pointing to it ("CLAUDE_CONFIG_DIR" or "CODEX_CONFIG_DIR").
        """
        # Config dir lives inside the caller / user / workspace dir (no
        # separate mount), or a session's own scratch engine home's
        if self.cfg.external_home or self.cfg.wrapup_home is not None or self.cfg.engine_home:
            scope_root = EXTERNAL_SANDBOX_HOME
        elif self.cfg.username:
            scope_root = layout.virtual_user_root(self.cfg.username)
        else:
            scope_root = layout.V_WORKSPACE
        cfg_dir = f"{scope_root}/{config_dir_name}"

        from core.sandbox import session_config_dir as scd
        env = {
            config_env_var: cfg_dir,
            # pnpm 12 refuses an offline install without its metadata cache,
            # which it keeps under HOME (a fresh /tmp every run): the cache
            # lives beside the store in the scope's tree (`.cache` is a
            # workspace-sync skip dir).
            "PNPM_CONFIG_CACHE_DIR": f"{scope_root}/.cache/pnpm",
            # The command wrappers (bound read-only, so nothing a session
            # writes can precede the system dirs), then the system dirs only
            # (a user home isn't mounted).
            "PATH": f"{cfg_dir}/{scd.WRAPPER_DIR}:{scd.SANDBOX_PATH}",
            "HOME": "/tmp",  # CLI needs a writable HOME for temp files
            # Where git's credential helper and the wrappers live, and the
            # interpreter that runs them (the same resolved path the hook
            # commands name): the git entries the resolver writes expand
            # these through the shell.
            "OTO_HOOKS_DIR": cfg_dir,
            "OTO_HOOK_PY": scd.sandbox_python(),
        }
        # The Claude CLI refuses `--dangerously-skip-permissions` when it runs as
        # root. No default topology runs the proxy as root — bare-metal T1 is the
        # operator's user, and the containerised T2/cloud proxy is uid 1000 — but
        # if an operator runs the image as root, the agent would be root and hit
        # that guard. Every agent is already wrapped in our own bwrap (mount + PID
        # namespace + pivot_root), so the guard is redundant; IS_SANDBOX=1 is the
        # CLI's sanctioned signal that an external sandbox owns isolation. Gated on
        # uid 0 so every non-root path (the norm) keeps a byte-identical env.
        if os.getuid() == 0:
            env["IS_SANDBOX"] = "1"
        return env

    # -----------------------------------------------------------------------
    # Internal mount builders
    # -----------------------------------------------------------------------


    def workspace_mount_table(self) -> list[Mount]:
        """The role mount table (``_role_mount_table``) with the session's
        hook scripts and stdio interceptor bound read-only over their copies
        (``_hook_file_binds``)."""
        mounts = self._role_mount_table()
        return mounts + self._hook_file_binds(mounts)

    def _hook_file_binds(self, mounts: list[Mount]) -> list[Mount]:
        """A read-only bind of each hook script and the stdio interceptor
        (``session_config_dir.HOOK_SCRIPTS``), and of the file that names the
        hooks (``settings.json`` in a ``.claude`` dir, ``hooks.json`` in a
        ``.codex`` dir), over its copy in every CLI state dir the table
        leaves read-write: the session's own ``.claude``/``.codex`` (a
        user's, an external caller's, or the agent scope's inside
        ``/workspace``). The copies sit in a writable tree, so without this a
        session could rewrite the hook that judges its own tools, or the file
        that names it (the path policy refused it, the kernel did not). The
        CLI's own settings writes then fail and last for the session only. A
        dir the table masks or binds read-only takes none; a file that is
        missing, a link, or under a linked path is skipped. A state dir that
        is not a mount of its own (the agent scope's, inside ``/workspace``)
        is first bound onto itself read-write: a mountpoint cannot be renamed
        away, so the session cannot swap in a dir of its own around the
        binds. A bind holds only while the host file is not replaced (a
        rename over a mountpoint detaches it in every other namespace), so
        these files' content stays a function of the scope's paths and the
        build (``session_config_dir.write_if_changed``). A ``.codex`` dir
        also takes the null device, read-only, over its instruction file
        names (``_shared_text_masks``)."""
        from core.sandbox.session_config_dir import (
            _INTERCEPTOR_SRC,
            HOOK_FILE_OF_STATE_DIR,
            HOOK_SCRIPTS,
            WRAPPER_DIR,
        )

        def cover(path: str) -> Mount | None:
            best = None
            for m in mounts:
                if path == m.sandbox or path.startswith(m.sandbox.rstrip("/") + "/"):
                    if best is None or len(m.sandbox) >= len(best.sandbox):
                        best = m
            return best

        state = (".claude", ".codex")
        dirs: list[str] = []
        for m in mounts:
            if m.sandbox.rsplit("/", 1)[-1] in state:
                dirs.append(m.sandbox)
            elif m.sandbox == layout.V_WORKSPACE:
                dirs.extend(f"{m.sandbox}/{sub}" for sub in state)
        names = (*HOOK_SCRIPTS, _INTERCEPTOR_SRC.name)
        binds: list[Mount] = []
        for d in dict.fromkeys(dirs):
            c = cover(d)
            if c is None or not c.rw:
                continue
            # The mount's own source was vetted by the table; nothing below
            # it may be a link (the tree is writable from inside).
            root = Path(os.path.realpath(c.host))
            rel = d[len(c.sandbox):].lstrip("/")
            host_dir = root.joinpath(*rel.split("/")) if rel else root
            if os.path.realpath(host_dir) != str(host_dir):
                continue
            files = [host_dir / name
                     for name in (*names, HOOK_FILE_OF_STATE_DIR[d.rsplit("/", 1)[-1]])]
            files = [f for f in files
                     if f.is_file() and not f.is_symlink() and os.path.realpath(f) == str(f)]
            if not files:
                continue
            if c.sandbox != d:
                binds.append(Mount(str(host_dir), d, True))
            binds.extend(Mount(str(f), f"{d}/{f.name}", False) for f in files)
            binds.extend(self._shared_text_masks(self._agent_root_real(), host_dir, d))
            # The command wrappers' directory sits first on the session's
            # PATH: read-only as a whole, so nothing can be added beside
            # them (a planted interpreter there would run in place of the
            # hooks'), and a mountpoint cannot be renamed away.
            wrappers = host_dir / WRAPPER_DIR
            if (wrappers.is_dir() and not wrappers.is_symlink()
                    and os.path.realpath(wrappers) == str(wrappers)):
                binds.append(Mount(str(wrappers), f"{d}/{WRAPPER_DIR}", False))
        return binds

    @staticmethod
    def _shared_text_masks(root: Path, host_dir: Path, d: str) -> list[Mount]:
        """The host's null device, read-only, over each instruction file name
        of a Codex home (``session_text.SHARED_TEXT_FILES``). Codex reads
        those files for every session of the home at every turn and skips one
        that is empty or not a file, so a session cannot leave text there for
        the others. The mountpoint, an empty file made here, stays in the
        home, which no start removes (``session_text.remove_shared_text``:
        removing it would detach the mask in every running sandbox of the
        home) and which a build without the mask writes into as before.
        Anything else at the name was planted (a link, a dir, a FIFO, a
        socket, a device) and is removed: bwrap would follow a link and
        cannot mount a file over a dir, and a FIFO would block the open.
        Every name is reached beneath ``root``, the agent's real folder the
        table's sources come from, with no link followed at any part: a home
        that is not a plain folder there gets its masks and no file work."""
        from core.layers.codex import session_text
        from services.infra import safe_fs
        # By the state dir's name, as HOOK_FILE_OF_STATE_DIR: the engine that
        # reads instruction files from its home.
        names = {".codex": session_text.SHARED_TEXT_FILES}.get(d.rsplit("/", 1)[-1], ())
        masks: list[Mount] = []
        try:
            base = safe_fs.rel_under(host_dir, root)
        except OSError:
            base = None
            if names:
                logger.warning("sandbox: Codex home %s is outside the agent's folder, masks only", host_dir)
        for name in names:
            if base is not None:
                SandboxBuilder._ready_mask_point(root, f"{base}/{name}" if base else name)
            masks.append(Mount(os.devnull, f"{d}/{name}", False))
        return masks

    @staticmethod
    def _ready_mask_point(root: Path, rel: str) -> None:
        """The mask's mountpoint at ``rel`` beneath ``root``: anything that is
        not a regular file is removed first, then an empty owner-writable file
        is made if none is there (bwrap would make it read-only, 0444, which a
        build without the mask cannot write). Never blocking, judged on the
        descriptor; a refusal leaves the name as it is."""
        from services.infra import safe_fs
        try:
            st = safe_fs.lstat_beneath(root, rel)
        except OSError:
            st = None
        if st is not None and not stat.S_ISREG(st.st_mode):
            with contextlib.suppress(OSError):
                if stat.S_ISDIR(st.st_mode):
                    safe_fs.rmtree_beneath(root, rel)
                else:
                    safe_fs.unlink_beneath(root, rel)
                logger.warning("sandbox: removed %s planted in a Codex home", rel)
        with contextlib.suppress(OSError):
            fd = safe_fs.open_beneath(root, rel, os.O_RDONLY | os.O_CREAT, 0o600)
            try:
                if stat.S_ISREG(os.fstat(fd).st_mode):
                    os.fchmod(fd, 0o600)
            finally:
                os.close(fd)

    def _role_mount_table(self) -> list[Mount]:
        """Role-dependent workspace mounts (3-tier per-agent model), as the
        ordered list of :class:`Mount` decisions.

        Mount table:

        | Role                  | /config | /knowledge | /workspace | /users/{u} |
        |-----------------------|---------|------------|------------|------------|
        | manager (= owner)     | RW      | RW         | RW         | RW*        |
        | editor                | (none)  | RO         | RW         | RW*        |
        | viewer                | (none)  | RO         | RO         | RW*        |
        | admin                 | RW      | RW         | RW         | RW*        |
        | agent-scope (no user) | (none)  | RO†        | RW         | (none)     |

        *RW* = the user dir's ROOT is RO; its known subdirs (workspace/,
        context/, .claude/, .codex/) stack RW on top, so stray root-level
        files are kernel-denied for every tool path.

        †RO. No OAuth token file is mounted into any sandbox: a
        ``.credentials`` directory under /knowledge or under /users/{u} is
        masked with an empty read-only dir (``_credentials_mask``), for
        what earlier releases may have left there.

        `/config/` is **owner-only** — editor + viewer don't see it at all.
        Config shapes agent BEHAVIOR (prompt, MCP wiring, auto-loaded
        context). That's owner curation, not workspace collaboration.

        Kernel-level enforcement is the single source of truth for file
        access — sidesteps Codex's hook bypass for non-Bash tools. The
        application-level path_policy is defense-in-depth that runs in the
        Claude Code hook and in the Direct-LLM builtin gate. The Direct-LLM
        file tools run in a program inside the session's sandbox, under
        these very mounts, and their platform half reads this list as text
        for the sentence of an early refusal.
        """
        mounts: list[Mount] = []
        if self.cfg.app_mounts is not None:
            # An app server: its own table, no workspace, no users, no
            # config (APPS.md "The app sandbox").
            return list(self.cfg.app_mounts)
        role = self.cfg.role
        username = self.cfg.username
        # Every source below is spelled from the agent's resolved folder, the
        # root the launcher's --beneath names. The folder itself is
        # platform-owned (safe to resolve); everything BELOW it that we bind
        # from an agent-writable subtree must additionally prove it carries
        # no symlinked component (_verified_literal_path), is created with
        # no link followed (_mkdirs_held), and reaches bwrap as a descriptor.
        agent_root_real = self._agent_root_real()
        agent_dir_path = agent_root_real
        agent_dir = str(agent_dir_path)

        # Defensive: bwrap --bind fails if source doesn't exist. The agent
        # template creation (agent_store.create_agent) now creates knowledge/
        # up front, but legacy agents won't have it until the one-shot
        # role_v2 migration runs. Creating it here is cheap insurance.
        _mkdirs_held(agent_root_real, layout.KNOWLEDGE)

        # Belt-and-braces: bind this agent's quota scopes to their XFS project
        # IDs before the RW bind mounts below expose the tree to writes. Free
        # when the kernel quota tier is off (a single flag check); idempotent +
        # process-memoized when on, so no per-build cost after the first session.
        try:
            from services.infra import storage_quota
            # The shared scope is metered for every agent (all modes the same);
            # the per-user scope only exists for a user-scope mount (a Shared-only
            # mount has username="" → no per-user dir to bound).
            storage_quota.ensure_scope(self.cfg.agent_name, "shared")
            if username:
                storage_quota.ensure_scope(self.cfg.agent_name, "user", username)
            if self.cfg.external_home:
                # Caller trees are metered as their own per-agent bucket so a
                # caller cannot fill the agent's shared one.
                storage_quota.ensure_scope(self.cfg.agent_name, "external")
        except (ImportError, psycopg.Error, RuntimeError):
            pass  # never block a sandbox build on quota assignment

        lib_attachments = self.cfg.knowledge_libraries

        def _mirror_host(src: str, subdir: str) -> str:
            # bwrap --bind fails on a missing source, and under an RO parent
            # bind it cannot create the mountpoint either — host-side mkdir
            # is the same insurance as the knowledge/ mkdir above.
            # Symlink-refusing: the mirror chain lives under the RW-bindable
            # /knowledge, so an owner-tier agent could have replaced a
            # component with a symlink (detach/re-attach leaves plain dirs
            # behind). Refused early here, then created with no link
            # followed, so a swap after the check fails the build loudly
            # rather than create dirs at, or bind, the target.
            parts = [layout.KNOWLEDGE, "shared", *Path(src).parts]
            if subdir:
                parts += Path(subdir).parts
            if _verified_literal_path(agent_root_real, *parts) is None:
                raise RuntimeError(
                    f"Refusing sandbox build: knowledge mirror path "
                    f"knowledge/shared/{src}{'/' + subdir if subdir else ''} "
                    f"contains a symlinked component (possible tampering)"
                )
            return str(_mkdirs_held(agent_root_real, *parts))

        def _mirror_dest(src: str, subdir: str) -> str:
            return (f"{layout.V_KNOWLEDGE}/shared/{src}/{subdir}" if subdir
                    else f"{layout.V_KNOWLEDGE}/shared/{src}")

        config_visible = self.cfg.config_visible
        if config_visible is None:
            # Not explicitly resolved (direct construction / pre-visibility-modes
            # caller): historical behavior — /config for an owner-tier role with
            # a real mount user (user-scope manager/admin).
            config_visible = roles.can_manage(role) and bool(username)
        mount_shared = self.cfg.mount_shared

        if not username and self.cfg.wrapup_home is not None:
            return self._wrapup_mounts(agent_root_real, agent_dir)

        if not username and self.cfg.external:
            # An outside session (a caller who is not a platform user): its
            # own home, the knowledge folders a manager picked, no shared
            # workspace, no knowledge root.
            return self._apply_read_only(self._outside_mounts(
                agent_root_real, lib_attachments, _mirror_host, _mirror_dest))

        if not username and not mount_shared:
            # Agent scope on an agent whose mode has no shared space
            # (Personal-only): nothing shared to bind; attached libraries
            # mirror one by one, read-only, as the user branch does.
            for _src, _subdir, _writable in lib_attachments:
                mounts.append(Mount(_mirror_host(_src, _subdir), _mirror_dest(_src, _subdir), False))
            return self._apply_read_only(mounts)

        if not username:
            # Agent-scope MOUNT — a service session (task / trigger /
            # meeting) OR a Shared-only HUMAN chat (``mount_username==""`` for
            # both). Role-aware: below the workspace tier (a Shared-only
            # viewer, or a word the table does not know) the workspace is
            # read-only. Service sessions force role=manager +
            # config_visible=False, so this stays byte-identical to the
            # pre-visibility-modes agent branch (/workspace RW + /knowledge RO).
            if roles.can_write_workspace(role):
                mounts.append(Mount(f"{agent_dir}/{layout.WORKSPACE}", layout.V_WORKSPACE, True))
            else:
                mounts.append(Mount(f"{agent_dir}/{layout.WORKSPACE}", layout.V_WORKSPACE, False))
            if not roles.can_edit(role) or self.cfg.app_press:
                # A person below the editor tier never runs from the agent's
                # own CLI state (the engines refuse such a session,
                # session_config_dir.refuse_session_on_agent_state): a script
                # run as them, and every app button, sees it masked.
                mounts.extend(self._agent_state_masks(agent_root_real))
            if config_visible or self.cfg.knowledge_rw:
                # Owner-tier human in the agent scope (curates knowledge +
                # config) — or a manager-provenance task fire (knowledge_rw:
                # knowledge RW, /config deliberately NOT mounted).
                mounts.append(Mount(f"{agent_dir}/{layout.KNOWLEDGE}", layout.V_KNOWLEDGE, True))
                if config_visible:
                    mounts.append(Mount(f"{agent_dir}/{layout.CONFIG}", layout.V_CONFIG, True))
                # Read-only library mirrors stay kernel-RO under the RW
                # parent — one nested bind per library SUBTREE.
                for _src, _subdir, _writable in lib_attachments:
                    if not _writable:
                        mounts.append(Mount(_mirror_host(_src, _subdir),
                                            _mirror_dest(_src, _subdir), False))
            else:
                # Knowledge is universal + RO for non-owners / service sessions
                # (mirrors ride the RO parent — no nested binds needed).
                mounts.append(Mount(f"{agent_dir}/{layout.KNOWLEDGE}", layout.V_KNOWLEDGE, False))
            mounts.extend(self._credentials_mask(
                agent_root_real, layout.KNOWLEDGE, layout.CREDENTIALS_DIR,
                sandbox=f"{layout.V_KNOWLEDGE}/{layout.CREDENTIALS_DIR}"))
            if self.cfg.engine_home:
                # The agent's own phone line: its scratch engine home at
                # /caller, the agent scope's own engine state and its shared
                # memory masked (anyone who dials acts as the agent).
                mounts.append(Mount(self.cfg.engine_home, EXTERNAL_SANDBOX_HOME, False))
                mounts.extend(_engine_state_mounts(agent_root_real, Path(self.cfg.engine_home)))
                if roles.can_edit(role):
                    mounts.extend(self._agent_state_masks(agent_root_real))
                mounts.extend(self._credentials_mask(
                    agent_root_real, layout.KNOWLEDGE, "memory", sandbox=f"{layout.V_KNOWLEDGE}/memory"))
            return self._apply_read_only(mounts)

        # User-scope MOUNT — the session has its own personal dir. The dir
        # ROOT is read-only with the known subdirs stacked RW on top: agents
        # kept scattering stray files next to workspace/ and context/ (the
        # dashboard file browser shows this root), and only the kernel stops
        # Codex native writes + Bash, which both bypass the permission hook.
        # Unknown future subdirs stay visible (RO) — a clean EROFS beats a
        # silent stray. Mirrored by the path-policy write allowlist
        # (auth/path_policy.py::_USER_DIR_WRITABLE_SUBDIRS).
        user_dir = layout.user_dir(agent_root_real, username)
        # Codex's own Linux sandbox (a nested bwrap) mounts read-only tmpfs
        # over <writable-root>/{.git,.agents,.codex,.aws} (.aws since 0.160)
        # for every writable root, cwd included — and bwrap must CREATE a
        # missing mountpoint, which this deliberately read-only root refuses
        # ("Can't mkdir /users/<u>/.aws: Read-only file system": the helper
        # dies, codex runs the command without its own layer). Re-read the
        # list on every Codex bump. Pre-create the ones codex doesn't make
        # itself as EMPTY dirs — git treats a bare .git dir as "not a git
        # repository"; .codex is session_config_dir's job, RW-bound below.
        for sub in (".git", ".agents", ".aws"):
            _mkdirs_held(agent_root_real, layout.USERS, username, sub)
        v_user = layout.virtual_user_root(username)
        mounts.append(Mount(str(user_dir), v_user, False))
        for sub in layout.USER_SUBDIRS:
            _mkdirs_held(agent_root_real, layout.USERS, username, sub)
            mounts.append(Mount(str(user_dir / sub), f"{v_user}/{sub}", True))
        # CLI state dirs (session's own is pre-created by the layer). An app
        # button runs from none of them: masked over the read-only root.
        if self.cfg.app_press:
            mounts.extend(self._state_masks(agent_root_real, (layout.USERS, username), v_user))
        else:
            for sub in (".claude", ".codex"):
                if (user_dir / sub).is_dir():
                    mounts.append(Mount(str(user_dir / sub), f"{v_user}/{sub}", True))
        mounts.extend(self._credentials_mask(
            agent_root_real, layout.USERS, username, layout.CREDENTIALS_DIR,
            sandbox=f"{v_user}/{layout.CREDENTIALS_DIR}"))

        # /config — owner-tier only (the agent's behavior layer). Hoisted out of
        # the role branch so Personal-only managers (no shared dirs) still get it.
        if config_visible:
            mounts.append(Mount(f"{agent_dir}/{layout.CONFIG}", layout.V_CONFIG, True))

        # Shared workspace + knowledge — present only when the agent's mode
        # offers the agent scope. Personal-only (``mount_shared=False``) omits
        # BOTH: it is fully private, no shared collaboration surface.
        if mount_shared:
            if roles.can_manage(role):
                # Owner tier: knowledge RW (reference library) + workspace RW.
                mounts.append(Mount(f"{agent_dir}/{layout.KNOWLEDGE}", layout.V_KNOWLEDGE, True))
                mounts.append(Mount(f"{agent_dir}/{layout.WORKSPACE}", layout.V_WORKSPACE, True))
                # Read-only library mirrors stay kernel-RO under the RW
                # parent — one nested bind per library SUBTREE.
                for _src, _subdir, _writable in lib_attachments:
                    if not _writable:
                        mounts.append(Mount(_mirror_host(_src, _subdir),
                                            _mirror_dest(_src, _subdir), False))
            elif roles.can_write_workspace(role):
                # Editor / contributor: workspace RW (collaboration), knowledge RO.
                mounts.append(Mount(f"{agent_dir}/{layout.KNOWLEDGE}", layout.V_KNOWLEDGE, False))
                mounts.append(Mount(f"{agent_dir}/{layout.WORKSPACE}", layout.V_WORKSPACE, True))
            else:
                # Viewer (or unknown): both RO — SEE state + docs, mutate nothing.
                mounts.append(Mount(f"{agent_dir}/{layout.KNOWLEDGE}", layout.V_KNOWLEDGE, False))
                mounts.append(Mount(f"{agent_dir}/{layout.WORKSPACE}", layout.V_WORKSPACE, False))
            mounts.extend(self._credentials_mask(
                agent_root_real, layout.KNOWLEDGE, layout.CREDENTIALS_DIR,
                sandbox=f"{layout.V_KNOWLEDGE}/{layout.CREDENTIALS_DIR}"))
            mounts.extend(self._agent_state_masks(agent_root_real))
        elif lib_attachments:
            # Personal-only: no shared /knowledge exists, but attached
            # libraries still mount — each mirror individually, ALWAYS
            # read-only (a personal-only agent has no knowledge-curation
            # surface, so the writable flag deliberately does not apply).
            # With no parent bind, bwrap creates the /knowledge/shared
            # chain inside the sandbox root itself.
            for _src, _subdir, _writable in lib_attachments:
                mounts.append(Mount(_mirror_host(_src, _subdir),
                                    _mirror_dest(_src, _subdir), False))

        return self._apply_read_only(mounts)

    def _credentials_mask(
        self, agent_root_real: Path, *rel_parts: str, sandbox: str,
    ) -> list[Mount]:
        """An empty read-only dir over a ``.credentials`` directory the parent
        bind would otherwise show. No OAuth token file is mounted into a
        sandbox, so a copy an earlier release left in the tree stays out of
        every session's view. Existence-guarded (bwrap cannot make a
        mountpoint under a read-only parent; no copy, nothing to hide) and
        symlink-refusing: a planted link refuses the build, as the state
        masks do. A link AT the name is never legitimate and, left in
        place, would refuse every later build of the agent: it is removed
        here and logged."""
        parent = _verified_literal_path(agent_root_real, *rel_parts[:-1])
        if parent is None:
            raise RuntimeError(
                f"Refusing sandbox build: {'/'.join(rel_parts[:-1])} contains a "
                "symlinked component (possible tampering)"
            )
        rel = "/".join(rel_parts)
        from services.infra import safe_fs
        try:
            if stat.S_ISLNK(safe_fs.lstat_beneath(agent_root_real, rel).st_mode):
                safe_fs.unlink_beneath(agent_root_real, rel, missing_ok=True)
                logger.warning("credentials: removed a symlink planted at %s", parent / rel_parts[-1])
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RuntimeError(
                f"Refusing sandbox build: {'/'.join(rel_parts[:-1])} changed underneath the "
                "build (possible tampering)"
            ) from exc
        cred_dir = _verified_literal_path(agent_root_real, *rel_parts)
        if cred_dir is None:
            raise RuntimeError(
                f"Refusing sandbox build: {'/'.join(rel_parts)} contains a "
                "symlinked component (possible tampering)"
            )
        if not cred_dir.is_dir():
            return []
        return [Mount(str(empty_mount_dir()), sandbox, False)]

    def _agent_state_masks(self, agent_root_real: Path) -> list[Mount]:
        """An empty read-only dir over the agent scope's CLI state
        (``workspace/.claude``, ``workspace/.codex``) for a session that does
        not run from it: the hooks, settings and MCP config every agent-scope
        session executes, the subscription login and the session tokens they
        carry. Seen through /workspace, any role could read the tokens with an
        opaque command, and a workspace-tier role could rewrite a hook that a
        manager's task runs next. The mountpoints are made host-side (bwrap
        cannot make one under a read-only /workspace) and a planted link
        refuses the build."""
        return self._state_masks(agent_root_real, (layout.WORKSPACE,), layout.V_WORKSPACE)

    @staticmethod
    def _state_masks(agent_root_real: Path, base: tuple[str, ...], v_root: str) -> list[Mount]:
        """The empty read-only masks over ``<base>/.claude`` and
        ``<base>/.codex`` below the agent's folder, seen at ``v_root``."""
        masks: list[Mount] = []
        for sub in (".claude", ".codex"):
            if _verified_literal_path(agent_root_real, *base, sub) is None:
                raise RuntimeError(
                    f"Refusing sandbox build: {'/'.join(base)}/{sub} contains a "
                    "symlinked component (possible tampering)"
                )
            _mkdirs_held(agent_root_real, *base, sub)
            masks.append(Mount(str(empty_mount_dir()), f"{v_root}/{sub}", False))
        return masks

    # The CLI state dirs a judge still writes (its own session files);
    # everything else goes read-only.
    _READ_ONLY_KEEP_RW = (".claude", ".codex")

    def _apply_read_only(self, mounts: list[Mount]) -> list[Mount]:
        """The judge profile: the identity's table with every row read-only
        except the CLI state dirs (CHECKS.md). A no-op for every other
        session."""
        if not self.cfg.read_only:
            return mounts
        return [m if (not m.rw or m.sandbox.rstrip("/").rsplit("/", 1)[-1] in self._READ_ONLY_KEEP_RW)
                else Mount(m.host, m.sandbox, False) for m in mounts]

    def _outside_mounts(self, agent_root_real: Path, lib_attachments, mirror_host, mirror_dest) -> list[Mount]:
        """The whole mount table of an outside session (``SandboxConfig.
        external``: a caller who is not a platform user), in bwrap's
        later-bind order (the Direct file tools' text check agrees: it
        picks the longest prefix):

        1. each knowledge folder a manager picked (``outside_knowledge``,
           ``outside_picks.knowledge_parts``), read-only at its own place: a
           folder of the agent's own knowledge, or a folder inside it, at
           ``/knowledge/<path>``; an attached library, or a folder inside it,
           at its mirror path; no ``/knowledge`` root, so nothing else of it
           exists; a folder that is gone, linked on its way, outside every
           attachment or never offered binds nothing, and only a library's
           own root is made when missing;
        2. the person's own tree (``external_home``, a scratch one when the
           identity keeps none) at /caller: a read-only root; ``workspace/``
           and ``context/`` read-write only with the files pick at Read and
           write, else read-only; ``context/memory`` read-only always (the
           memory is read into the prompt and written after the
           conversation); the CLI state dirs read-write when present. Every
           bind source is verified symlink-free under the resolved agent root
           (the tree sits below agent-writable binds, ``_verified_literal_path``);
        3. the agent scope's CLI state masked, as for every session that does
           not run from it.

        No shared workspace, whatever the role word says.
        """
        from services.mcp import outside_picks
        cfg = self.cfg
        mounts: list[Mount] = []
        for pick in cfg.outside_knowledge:
            parts = outside_picks.knowledge_parts(pick)
            library = outside_picks.library_of(parts, lib_attachments) if parts else None
            if parts is None or (parts[0] == outside_picks.SHARED_ROOT and library is None):
                logger.warning("outside people: knowledge pick %r of agent %s is not a folder on offer; not bound",
                               pick, cfg.agent_name)
                continue
            if library is not None and parts[2:] == (tuple(library[1].split("/")) if library[1] else ()):
                mounts.append(Mount(mirror_host(*library), mirror_dest(*library), False))
                continue
            folder = _verified_literal_path(agent_root_real, layout.KNOWLEDGE, *parts)
            if folder is None or not folder.is_dir():
                logger.warning("outside people: knowledge folder %r of agent %s is missing or linked; not bound",
                               pick, cfg.agent_name)
                continue
            mounts.append(Mount(str(folder), f"{layout.V_KNOWLEDGE}/{'/'.join(parts)}", False))
        if cfg.external_home:
            mounts.extend(_caller_tree_mounts(
                agent_root_real, Path(cfg.external_home), files_write=cfg.outside_files_write))
            mounts.extend(_engine_state_mounts(agent_root_real, Path(cfg.external_home)))
        mounts.extend(self._agent_state_masks(agent_root_real))
        return mounts

    def _wrapup_mounts(self, agent_root_real: Path, agent_dir: str) -> list[Mount]:
        """The wrap-up after a conversation (``cfg.wrapup_home`` set), in
        bwrap's later-wins order: the shared workspace and the knowledge read
        only (the credential directory masked), the person's tree at /caller
        with workspace/ and context/ writable and its memory read only, the
        engine's state from the run's scratch home over /caller/.claude and
        /caller/.codex, and the agent's own CLI state masked. Never /config,
        never /users."""
        cfg = self.cfg
        mounts: list[Mount] = []
        if cfg.mount_shared:
            # A Personal-only agent has no shared space to read.
            mounts.append(Mount(f"{agent_dir}/{layout.WORKSPACE}", layout.V_WORKSPACE, False))
            mounts.append(Mount(f"{agent_dir}/{layout.KNOWLEDGE}", layout.V_KNOWLEDGE, False))
            mounts.extend(self._credentials_mask(
                agent_root_real, layout.KNOWLEDGE, layout.CREDENTIALS_DIR,
                sandbox=f"{layout.V_KNOWLEDGE}/{layout.CREDENTIALS_DIR}"))
        if cfg.wrapup_home:
            mounts.extend(_caller_tree_mounts(agent_root_real, Path(cfg.wrapup_home), files_write=True))
        elif cfg.engine_home:
            mounts.append(Mount(cfg.engine_home, EXTERNAL_SANDBOX_HOME, False))
        if cfg.engine_home:
            mounts.extend(_engine_state_mounts(agent_root_real, Path(cfg.engine_home)))
        mounts.extend(self._agent_state_masks(agent_root_real))
        return mounts


# Per-session config-directory setup lives in session_config_dir.py (the
# shared spine; the engines' own bodies sit in core/layers/<x>/config_dir.py);
# the funnel and the MCP config copy are re-exported here for the call-time
# local imports across the codebase.
from core.sandbox.session_config_dir import (  # noqa: F401
    ensure_persistent_agent_dir,
    prepare_mcp_config_for_sandbox,
)

# ---------------------------------------------------------------------------
# Resolve helpers
# ---------------------------------------------------------------------------

def mcp_dir_binds_from_config(mcp_config_path: str | Path | None) -> list[str]:
    """Derive the MCP dirs a session's sandbox must bind from its MCP config.

    Scans the session's generated MCP config (JSON for CLI/Direct, TOML for
    Codex — a plain-text prefix scan works for both) for paths under
    ``MCPS_DIR`` and returns the unique existing ``mcps/<category>/<name>``
    roots. Deriving from the CONFIG (not the registry) makes the mount set
    exactly what the session will spawn: force-included MCPs (meetings-mcp)
    are covered, credential-/context-excluded ones are not. ``mcps/.uv-python``
    (shared uv-fetched interpreters some venvs symlink into; no secrets) is
    appended whenever any MCP dir is bound. Any failure returns what was
    resolved so far — missing dirs just mean that MCP fails to spawn visibly,
    never a widened mount.
    """
    binds: list[str] = []
    if not mcp_config_path:
        return binds
    try:
        text = Path(mcp_config_path).read_text(encoding="utf-8")
    except OSError:
        return binds
    mcps_dir = app_config.MCPS_DIR.resolve()
    pattern = re.compile(
        re.escape(str(mcps_dir)) + r"/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)"
    )
    seen: set[str] = set()
    for m in pattern.finditer(text):
        # Two plain parts only: a `.` or `..` would name a folder outside
        # the tools folder (the proxy's own data among them).
        if {m.group(1), m.group(2)} & {".", ".."}:
            continue
        d = str(mcps_dir / m.group(1) / m.group(2))
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            binds.append(d)
    if binds:
        # Every interpreter folder, whatever this host's disk holds: the
        # session may run on a machine whose folders are its own, and the
        # render skips a source its host lacks.
        from services.mcp import mcp_installer
        binds.extend(str(d) for d in mcp_installer.interpreter_dirs(mcps_dir))
    return binds


_STATE_DIR_NAMES = (".claude", ".codex")


def _seen_at(table: list[Mount], host_path: str) -> str | None:
    """Where ``host_path`` shows inside the sandbox, or None when no mount
    shows it or a mount over that place hides it: a mask, or another folder
    bound there (the agent scope's engine folders for a session that does
    not run from them, ``_agent_state_masks``). bwrap mounts in the table's
    order, so the deepest place wins and a later mount the same place."""
    def deepest(path: str, key) -> Mount | None:
        best = None
        for m in table:
            at = key(m).rstrip("/")
            if path == key(m) or path.startswith(at + "/"):
                if best is None or len(key(m)) >= len(key(best)):
                    best = m
        return best
    source = deepest(host_path, lambda m: m.host)
    if source is None or source.host in (str(empty_mount_dir()), os.devnull):
        return None
    sandbox = source.sandbox.rstrip("/") + host_path[len(source.host.rstrip("/")):]
    shown = deepest(sandbox, lambda m: m.sandbox)
    if (shown is None or shown.host in (str(empty_mount_dir()), os.devnull)
            or shown.host.rstrip("/") + sandbox[len(shown.sandbox.rstrip("/")):] != host_path):
        return None
    return sandbox


def own_engine_home(ctx, host_config_dir) -> str:
    """The scratch engine home a session runs from instead of its scope's own
    (the config dir's parent): the wrap-up's after a conversation and the
    agent's own phone line's; "" for every other session."""
    if not (getattr(ctx, "wrapup", "") or getattr(ctx, "open_line", False)) or not host_config_dir:
        return ""
    return str(Path(host_config_dir).parent)


def _caller_tree_mounts(agent_root_real: Path, home: Path, *, files_write: bool) -> list[Mount]:
    """A person's tree at /caller: a read-only root, with ``workspace/`` and
    ``context/`` writable when ``files_write`` and ``context/memory/`` read
    only always. Refuses a tree that is not a plain directory under the
    agent's, or whose subdirs hold a symlinked component."""
    home_real = Path(os.path.realpath(home))
    if home_real != home or not home_real.is_relative_to(agent_root_real):
        raise RuntimeError(
            f"Refusing sandbox build: external home {home} is not a "
            "plain directory under the agent tree (possible tampering)"
        )
    rel_parts = home_real.relative_to(agent_root_real).parts
    for sub in (*layout.USER_SUBDIRS, f"{layout.CONTEXT}/memory"):
        if _verified_literal_path(agent_root_real, *rel_parts, *sub.split("/")) is None:
            raise RuntimeError(
                f"Refusing sandbox build: external home subdir {home / sub} "
                "contains a symlinked component (possible tampering)"
            )
        _mkdirs_held(agent_root_real, *rel_parts, *sub.split("/"))
    mounts = [Mount(str(home), EXTERNAL_SANDBOX_HOME, False)]
    if files_write:
        for sub in layout.USER_SUBDIRS:
            mounts.append(Mount(str(home / sub), f"{EXTERNAL_SANDBOX_HOME}/{sub}", True))
        mounts.append(Mount(str(home / layout.CONTEXT / "memory"),
                            f"{EXTERNAL_SANDBOX_HOME}/{layout.CONTEXT}/memory", False))
    return mounts


def _engine_state_mounts(agent_root_real: Path, home: Path) -> list[Mount]:
    """The engine's own state dirs of ``home`` (``.claude``, ``.codex``) at
    /caller, writable: the engine writes its session there."""
    home_real = Path(os.path.realpath(home))
    if not home_real.is_relative_to(agent_root_real):
        return []
    rel_parts = home_real.relative_to(agent_root_real).parts
    mounts = []
    for sub in (".claude", ".codex"):
        p = _verified_literal_path(agent_root_real, *rel_parts, sub)
        if p is not None and p.is_dir():
            mounts.append(Mount(str(p), f"{EXTERNAL_SANDBOX_HOME}/{sub}", True))
    return mounts


def resolve_sandbox_config(
    role: str,
    username: str,
    agent_name: str,
    is_admin_agent: bool,
    host_claude_dir: Path,
    *,
    user_sub: str = "",
    mcp_sandbox_mounts: list[SandboxMount] | None = None,
    extra_ro_binds: list[str] | None = None,
    net_forwards: list[str] | None = None,
    net_allow_hosts: list[str] | None = None,
    extra_egress_targets: list[str] | None = None,
    config_visible: bool | None = None,
    mount_shared: bool = True,
    knowledge_rw: bool = False,
    mcp_config_path: str | Path | None = None,
    mcp_dir_binds: list[str] | None = None,
    external: bool = False,
    external_home: str = "",
    read_only: bool = False,
    account_choices: dict[str, str] | None = None,
    attached_mcps: Iterable[str] | None = None,
    outside_files_write: bool = False,
    outside_knowledge: Iterable[str] = (),
    wrapup_home: str | None = None,
    engine_home: str = "",
    session_home: str = "",
    app_press: bool = False,
    own_copy: bool = False,
) -> SandboxConfig:
    """Build a SandboxConfig from session context.

    ``attached_mcps`` names the MCPs the session's build attached (the
    assigned set minus ``AgentConfig.excluded_mcps``): the egress carves
    routes for those alone, so a tool a session was refused (a kind's
    ``exclude_from``, the audience, the credentials, an outside pick) has no
    route either. None carves for every assigned MCP, as a unit test asks.
    ``outside_files_write`` and ``outside_knowledge`` are an outside
    session's picks (``SandboxConfig``); ``wrapup_home`` the wrap-up after a
    conversation's (``None``: not one); ``engine_home`` the scratch engine
    home of a session that does not run from its scope's (``own_engine_home``);
    ``session_home`` the home a session runs from (``SandboxConfig``);
    ``own_copy`` a session placed on a compute machine on another server,
    whose sandbox opens no tool address (``resolve_sandbox_egress``).

    ``external`` / ``external_home`` describe an EXTERNAL session (a phone
    caller who is not a platform user — see ``SandboxConfig``): the shared
    memory is masked and, with a home, the caller's own tree mounts at
    /caller.

    ``config_visible`` / ``mount_shared`` carry the visibility-modes decouple
    (``username`` here is the MOUNT username). Callers that resolve the agent's
    mode pass them explicitly; callers that don't get the historical behavior:
    ``config_visible`` derives from ``(owner-tier role AND a real mount user)``
    and ``mount_shared`` defaults True (every collaborative session has the
    shared dirs). This lets each of the four config builders adopt the explicit
    wiring independently without changing today's mounts.

    All three local layers (CLI / Codex / Direct-LLM) construct their sandbox
    through here, so the egress allow-set is resolved in this one place
    (isolation is mandatory). The caller may pass an explicit set (unit tests
    do); otherwise it's derived from the registry — the proxy hook port (always
    present) + T1 Docker-MCP loopback ports + T2 Docker-MCP container IPs + the
    enabled homelab MCP targets. ``user_sub`` resolves per-user targets (e.g.
    nextcloud). The proxy port is always present, so the set is never empty →
    the session is always wrapped; if resolution raises we fall back to the
    proxy port ONLY (a visible, degraded — but still isolated — session), never
    to host networking.

    ``mcp_config_path`` is the session's generated MCP config (JSON or TOML);
    the sandbox binds ONLY the MCP dirs referenced in it (see
    :func:`mcp_dir_binds_from_config`) — never the whole mcps/ tree.
    ``mcp_dir_binds`` overrides the derivation for layers with no config FILE
    (Direct-LLM's proxy-managed delivery passes its assigned stdio dirs).
    """
    if net_forwards is None:
        try:
            from services.mcp import mcp_registry
            # The chat's choice of account decides whose own endpoint a
            # per-person tool may reach (given only when the chat has one).
            net_forwards, _allow = mcp_registry.resolve_sandbox_egress(
                agent_name, user_sub=user_sub, extra_targets=extra_egress_targets,
                attached=attached_mcps,
                own_copy=own_copy,
                **({"account_choices": account_choices} if account_choices else {}),
            )
            if net_allow_hosts is None:
                net_allow_hosts = _allow
        except Exception:
            # Fail CLOSED, not open: fall back to the proxy hook port ONLY so
            # the session stays netns-wrapped (isolated). MCP/LLM targets may be
            # unreachable — a visible, degraded session — rather than silently
            # dropping to host networking. Never returns [] here.
            logger.exception(
                "netns: egress resolution failed for agent %s — wrapping with "
                "proxy port only (no MCP/target carve-outs)", agent_name)
            net_forwards = [str(app_config.PORT)]
    # Attached knowledge libraries — resolved HERE (the single funnel every
    # local layer builds through) so no layer call site needs to know about
    # them. Resolution failure logs loudly and mounts none: the parent
    # /knowledge bind still exposes mirror CONTENT read-appropriately; only
    # the nested kernel-RO overlay is lost, and the path-policy subtree gate
    # remains as defense-in-depth for that degraded window.
    try:
        from storage.knowledge import db_knowledge_libraries
        knowledge_libraries = [
            (a["source_agent"], a["subdir"] or "", bool(a["writable"]))
            for a in db_knowledge_libraries.attachments_for_consumer(agent_name)
        ]
    except Exception:
        logger.exception(
            "knowledge-library resolution failed for %s — mounting none",
            agent_name)
        knowledge_libraries = []
    # ``config_visible`` is forwarded as-is: None (the default) lets the mount
    # builder apply the historical derivation; an explicit bool from the
    # visibility resolver overrides it (e.g. a Shared-only owner-tier human).
    return SandboxConfig(
        role=role,
        username=username,
        agent_name=agent_name,
        is_admin_agent=is_admin_agent,
        host_agents_dir=app_config.AGENTS_DIR.resolve(),
        host_mcps_dir=app_config.MCPS_DIR.resolve(),
        host_claude_dir=host_claude_dir,
        config_visible=config_visible,
        mount_shared=mount_shared,
        knowledge_rw=knowledge_rw,
        knowledge_libraries=knowledge_libraries,
        mcp_sandbox_mounts=mcp_sandbox_mounts or [],
        extra_ro_binds=extra_ro_binds or [],
        net_forwards=net_forwards or [],
        net_allow_hosts=net_allow_hosts or [],
        mcp_dir_binds=(
            list(mcp_dir_binds) if mcp_dir_binds is not None
            else mcp_dir_binds_from_config(mcp_config_path)
        ),
        external=external,
        external_home=external_home or "",
        read_only=read_only,
        outside_files_write=bool(outside_files_write),
        outside_knowledge=tuple(outside_knowledge or ()),
        wrapup_home=wrapup_home,
        engine_home=engine_home or "",
        session_home=session_home or "",
        app_press=app_press,
    )
