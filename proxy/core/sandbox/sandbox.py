"""Bubblewrap (bwrap) sandbox engine for agent session isolation.

Pure, testable module. SandboxBuilder constructs bwrap command prefixes
based on a SandboxConfig. No side effects — only produces command lists.

ensure_persistent_claude_dir() creates and populates the persistent
.claude/ directory for a session (hooks, settings.json).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import config as app_config

logger = logging.getLogger("claude-proxy.sandbox")


def _verified_literal_path(root_real: Path, *parts: str) -> Path | None:
    """Resolve ``root_real/parts...`` and demand it IS that literal path.

    ``root_real`` must already be fully resolved (``os.path.realpath``).
    Returns the literal path when no component below the root is a symlink
    (``realpath == expected``), else ``None``. Required for every bind
    source / host-side dir operation under an agent-WRITABLE tree: an agent
    holding the parent RW in one session can plant a symlink there, and a
    follow at build/bind time moves the operation to the symlink's target
    on the HOST (the file-transfer B4 class — a planted
    ``knowledge/.credentials → /`` would otherwise become an RW bind of the
    host root in the next agent-scope session).
    """
    expected = root_real.joinpath(*parts)
    if os.path.realpath(expected) != str(expected):
        return None
    return expected

# System paths to mount read-only into every sandbox
_SYSTEM_RO_BINDS = [
    "/usr",
    "/bin",
    "/lib",
    "/sbin",
]

# Optional system paths (mount only if they exist on the host)
_SYSTEM_RO_OPTIONAL = [
    "/lib64",
    "/lib32",
    # Snap-installed binaries land at /snap/bin/{tool}. Mount when the
    # host has it so sandboxed agents can invoke gh / kubectl / etc. that
    # came from snap. No-op on non-snap hosts (Docker, most servers).
    "/snap/bin",
]

# Individual files to mount read-only
_SYSTEM_RO_FILES = [
    "/etc/resolv.conf",
    "/etc/ssl",
    "/etc/hosts",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    # System git config — set by `scripts/install-baseline-tools.sh` to
    # wire `/usr/local/bin/oto-git-credential-helper` as github.com's
    # credential helper so `git push` consults `GH_TOKEN` (injected via
    # manifest `env_injection`) without manual `gh auth setup-git`.
    # Mounted only when the host has it (existing existence check below).
    "/etc/gitconfig",
]

# Ubuntu 24.04+ AppArmor gate on unprivileged user namespaces: when this
# sysctl reads "1", only processes confined by a profile granting `userns`
# (or holding CAP_SYS_ADMIN) may create them — the exact capability the agent
# sandbox is built on. Module-level so tests can point it at a fixture.
_APPARMOR_USERNS_SYSCTL = Path("/proc/sys/kernel/apparmor_restrict_unprivileged_userns")


def _apparmor_userns_restricted() -> bool:
    """True when the host kernel restricts unprivileged userns via AppArmor."""
    try:
        return _APPARMOR_USERNS_SYSCTL.read_text().strip() == "1"
    except OSError:
        return False  # sysctl absent → not an Ubuntu-restricted kernel


# Debian's older gate on the same capability (predates the AppArmor sysctl,
# also present on Ubuntu kernels): 0 disables unprivileged userns creation
# kernel-wide. Module-level so tests can point it at a fixture.
_DEBIAN_USERNS_SYSCTL = Path("/proc/sys/kernel/unprivileged_userns_clone")


def _debian_userns_disabled() -> bool:
    """True when the Debian sysctl disables unprivileged user namespaces."""
    try:
        return _DEBIAN_USERNS_SYSCTL.read_text().strip() == "0"
    except OSError:
        return False  # sysctl absent → not a Debian-gated kernel


# ---------------------------------------------------------------------------
# Network namespace isolation (always on)
# ---------------------------------------------------------------------------
#
# Every LOCAL sandboxed session runs inside a pasta-managed network namespace
# (RFC1918 + the host's own subnet + cloud-metadata blackholed), with egress
# carved ONLY to the session's legitimate destinations (proxy hook port +
# configured MCP / docker-MCP / local-LLM targets — see resolve_sandbox_egress).
# There is no un-isolated sandbox mode: genuinely un-isolated execution is a
# remote machine. Un-resolvable egress fails CLOSED (refuses to spawn) — never
# a silent fall-back to host networking.

# The launcher that owns pasta orchestration (pasta is the OUTER process: it
# creates the netns, then spawns bwrap inside it).
# Lives in proxy/scripts/ and runs in the HOST mount namespace, before bwrap —
# referenced by absolute path so no PATH setup is needed on installs.
_NETNS_LAUNCHER = app_config.BASE_DIR / "scripts" / "oto-sandbox-net"

# In-netns DNS forwarder address used on hosts whose /etc/resolv.conf points
# at a loopback stub resolver (systemd-resolved's 127.0.0.53, Docker's embedded
# 127.0.0.11) — loopback addresses can't exist inside the netns, so pasta
# intercepts port 53 to this address and relays to the host's real resolver
# (same address rootless podman uses). The launcher carves a /32 route for it
# out of the 169.254.0.0/16 metadata blackhole; 169.254.169.254 stays
# unreachable via its more-specific /32 block.
_NETNS_DNS_FORWARD_ADDR = "169.254.1.1"


def netns_resolv_path() -> Path:
    """Generated resolv.conf bind-mounted over /etc/resolv.conf in netns mode.

    Written by netns_preflight() when the host resolver is a loopback stub;
    deleted when it isn't. Its existence is the single signal the builder
    uses to emit the bind swap + --dns-forward (no second decision point).
    """
    return app_config.SESSIONS_DIR / "netns-resolv.conf"


def _host_resolv_has_loopback_ns(resolv_path: str = "/etc/resolv.conf") -> bool:
    """True if any nameserver in the host resolv.conf is a loopback address.

    One loopback entry is enough to need the swap: glibc tries nameservers in
    order, so a dead stub first in line means 5s timeouts per lookup even if
    a reachable server follows.
    """
    import ipaddress
    try:
        text = Path(resolv_path).read_text()
    except OSError:
        return False
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver":
            try:
                if ipaddress.ip_address(parts[1]).is_loopback:
                    return True
            except ValueError:
                continue
    return False


# Result of the boot-time nested-namespace probe: None = not probed (yet),
# True/False afterwards. Read by the codex layer to warn per local session on
# hosts where the engine-owned inner sandbox cannot start.
_nested_sandbox_ok: bool | None = None


def nested_sandbox_ok() -> bool | None:
    """Boot probe result: can a bwrap nest inside the sandbox stack?"""
    return _nested_sandbox_ok


def _nested_sandbox_probe() -> str | None:
    """Probe that a SECOND bwrap can start inside the full sandbox stack.

    Mirrors the real runtime shape — the pasta netns launcher wrapping bwrap
    with our exact namespace flags — and runs a minimal inner bwrap where the
    engine's helper would sit. System bwrap stands in for Codex's npm-vendored
    copy (whose install path isn't knowable at proxy boot);
    scripts/sandbox-doctor.sh probes the vendored binary itself.

    Returns None when the nested level works, else a short failure detail.
    Diagnostic only: callers must treat a failure as a warning, never fatal.
    """
    bwrap = shutil.which("bwrap")
    if not bwrap:  # unreachable after the missing-tools gate; belt-and-braces
        return "bwrap not on PATH"
    argv = [str(_NETNS_LAUNCHER), "--block-private", "--",
            bwrap, "--unshare-pid", "--die-with-parent", "--share-net"]
    if os.getuid() != 0:
        argv += ["--unshare-user", "--uid", str(os.getuid()),
                 "--gid", str(os.getgid())]
    argv += ["--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--",
             bwrap, "--ro-bind", "/", "/", "--", "/bin/true"]
    try:
        probe = subprocess.run(
            argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20,
        )
    except subprocess.TimeoutExpired:
        return "nested probe timed out after 20s"
    except Exception as e:  # noqa: BLE001 — diagnostic-only path
        return f"nested probe failed to run: {e}"
    if probe.returncode == 0:
        return None
    detail = (probe.stderr or b"").decode(errors="replace").strip()
    return detail[-400:] or f"exit {probe.returncode}"


def netns_preflight() -> None:
    """Startup gate for the always-on sandbox network isolation — called at boot.

    Hard-fails when the host can't enforce the isolation, so the misconfig is
    caught at deploy rather than silently running agents unisolated (there is no
    un-isolated mode). Verifies the tools exist AND that an unprivileged
    user+net namespace can actually be created (a binary may be present yet
    namespace creation blocked by the container's seccomp/userns profile — that
    would otherwise pass and fail every session at spawn). Also materializes the
    stub-resolver resolv.conf swap file so every later build_command_prefix()
    call is a pure argv transform.
    """
    missing = [tool for tool in ("pasta", "ip", "bwrap")
               if shutil.which(tool) is None]
    if not os.access(_NETNS_LAUNCHER, os.X_OK):
        missing.append(str(_NETNS_LAUNCHER))
    if missing:
        raise RuntimeError(
            "Sandbox network isolation is mandatory but the host is missing: "
            f"{', '.join(missing)}. Install the `passt` package (provides "
            "pasta; see VERSIONS.md for the pin) and `iproute2`. Refusing to "
            "start: agents would otherwise run without network isolation."
        )

    # Capability probe: can we actually create an unprivileged user+net
    # namespace? `unshare -Urn true` exercises exactly the namespaces the
    # sandbox needs; a present-but-blocked kernel/profile fails here at boot
    # instead of at every session spawn.
    try:
        probe = subprocess.run(
            ["unshare", "-Urn", "true"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10,
        )
    except FileNotFoundError:
        # `unshare` (util-linux) absent. This used to skip the probe entirely —
        # letting a namespace-denying host boot and then fail every session
        # spawn — so probe with bwrap itself instead (bwrap presence is already
        # gated above; --unshare-all exercises user+net+mount creation).
        logger.warning(
            "netns preflight: `unshare` (util-linux) is missing — probing "
            "namespace support with bwrap directly. Install util-linux for "
            "the canonical probe."
        )
        try:
            probe = subprocess.run(
                ["bwrap", "--unshare-all", "--ro-bind", "/", "/", "--",
                 "/bin/true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=10,
            )
        except Exception as e:
            raise RuntimeError(
                f"Sandbox netns capability probe failed to run: {e}. The host "
                "must allow unprivileged user+network namespaces."
            )
    except Exception as e:
        raise RuntimeError(
            f"Sandbox netns capability probe failed to run: {e}. The host must "
            "allow unprivileged user+network namespaces."
        )
    if probe is not None and probe.returncode != 0:
        detail = (probe.stderr or b"").decode(errors="replace").strip()
        if _apparmor_userns_restricted():
            # The Ubuntu 24.04+ default. Name the exact cause and the scoped
            # remedy — the generic advice people find is "set the sysctl to
            # 0", which disables the hardening for the whole host.
            raise RuntimeError(
                "Sandbox network isolation is mandatory but this host denies "
                "unprivileged user namespaces via AppArmor "
                "(kernel.apparmor_restrict_unprivileged_userns=1 — the "
                f"Ubuntu 24.04+ default; `unshare -Urn` failed: {detail}). "
                "Fix, containerised install: run "
                "`sudo scripts/setup-apparmor-userns.sh` on the Docker HOST "
                "(installs the scoped `otodock_userns` AppArmor profile), then "
                "restart via scripts/compose.sh — it selects the profile "
                "automatically (standalone `docker compose`: set "
                "OTODOCK_APPARMOR_PROFILE=otodock_userns in .env). Bare-metal "
                "install: the distro AppArmor profiles for bwrap/unshare/passt "
                "must be present (Ubuntu's `apparmor` package ships them). Do "
                "NOT set the sysctl to 0 — that disables the protection for "
                "the entire host."
            )
        if _debian_userns_disabled():
            raise RuntimeError(
                "Sandbox network isolation is mandatory but unprivileged user "
                "namespaces are disabled on this host "
                "(kernel.unprivileged_userns_clone=0 — the Debian hardening "
                f"knob; probe failed: {detail}). Re-enable it "
                "(`sysctl kernel.unprivileged_userns_clone=1` + a "
                "/etc/sysctl.d drop-in) — the sandbox is built on unprivileged "
                "user namespaces."
            )
        raise RuntimeError(
            "Sandbox network isolation is mandatory but this host cannot create "
            f"an unprivileged user+network namespace (`unshare -Urn` failed: {detail}). "
            "Enable unprivileged user namespaces (and, in a container, relax the "
            "seccomp/userns profile)."
        )

    # Nested-level probe (diagnostic, never boot-fatal): the Codex engine ships
    # its own bubblewrap and nests it INSIDE our sandbox for every shell
    # command (its "fs sandbox helper"). A host can pass the one-level gate
    # above yet deny the second level — e.g. Ubuntu 24.04's AppArmor userns
    # restriction covers /usr/bin/bwrap via distro profiles but not what runs
    # a level deeper — and the only runtime symptom is every Codex command
    # degrading to unsandboxed retries + permission prompts, paraphrased by
    # the model as "the local sandbox could not start" (2026-08-12 incident).
    global _nested_sandbox_ok
    nested_detail = _nested_sandbox_probe()
    _nested_sandbox_ok = nested_detail is None
    if nested_detail is None:
        logger.info("netns: nested-namespace probe OK (engine inner sandboxes can start)")
    if nested_detail is not None:
        logger.warning(
            "Sandbox nested-namespace probe FAILED — the agent sandbox itself "
            "works, but a bwrap nested inside it cannot start (%s). Local "
            "Codex sessions lose their engine-side inner sandbox: shell "
            "commands degrade to unsandboxed retries behind permission "
            "prompts (Claude sessions are unaffected). Run "
            "scripts/sandbox-doctor.sh on this host to pinpoint the denial.",
            nested_detail,
        )

    resolv = netns_resolv_path()
    if _host_resolv_has_loopback_ns():
        resolv.parent.mkdir(parents=True, exist_ok=True)
        resolv.write_text(
            "# Generated by otodock (sandbox network isolation): the host's\n"
            "# resolver is a loopback stub that cannot exist inside the\n"
            "# agent netns — pasta forwards this address to the real one.\n"
            f"nameserver {_NETNS_DNS_FORWARD_ADDR}\n"
        )
        logger.info(
            "netns: host uses a loopback stub resolver — sessions get a "
            f"generated resolv.conf via pasta --dns-forward {_NETNS_DNS_FORWARD_ADDR}"
        )
    elif resolv.exists():
        resolv.unlink()


def cli_version_preflight() -> None:
    """Warn (don't fail) if the proxy host's claude/codex drift from the pins.

    The installer (`scripts/install-baseline-tools.sh`) pins + upgrades these and
    satellites reconcile on auth, but a manual `npm i -g …@latest` (or a CLI's
    own updater) can still drift the PROXY host — exactly the dev-box drift that
    motivated the pin-and-freeze. Catch it at boot. Warn-only: a drifted CLI
    usually still runs, and we never want to block startup on it.
    """
    import re
    import subprocess

    checks = (
        ("Claude Code", app_config.CLAUDE_BIN, app_config.PINNED_CLAUDE_CODE_VERSION),
        ("Codex", app_config.CODEX_BIN, app_config.PINNED_CODEX_VERSION),
    )
    for label, bin_name, want in checks:
        if not want:
            continue
        exe = shutil.which(bin_name)
        if not exe:
            logger.warning("CLI pin: %s (%s) not found on PATH", label, bin_name)
            continue
        try:
            out = subprocess.run(
                [exe, "--version"], capture_output=True, text=True, timeout=15,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        m = re.search(r"(\d+\.\d+\.\d+)", out or "")
        have = m.group(1) if m else ""
        if have and have != want:
            logger.warning(
                "CLI pin DRIFT: %s is %s but VERSIONS.md pins %s — run "
                "scripts/install-baseline-tools.sh (or `npm i -g <pkg>@%s`) to "
                "reconcile the proxy host.",
                label, have, want, want,
            )


def cli_install_ro_binds(bin_path: str) -> list[str]:
    """RO bind dirs that make a CLI installed OUTSIDE the system mounts
    (e.g. a user-prefix npm install under ``~/.npm-global``) runnable inside
    the sandbox: the binary's directory (shim/symlink location) plus the
    resolved package root for npm JS shims. Paths already under the system
    binds (/usr, …) just re-bind harmlessly. Shared by the CLI and Codex
    layers so both binaries get identical treatment.
    """
    if not bin_path or "/" not in bin_path:
        return []  # bare name → PATH-resolved inside the sandbox (system dirs)
    dirs = [os.path.dirname(bin_path)]
    real = os.path.realpath(bin_path)
    if real != os.path.abspath(bin_path):
        # npm shim: bin/<cli> symlinks into lib/node_modules/<pkg>/… — mount
        # the package root too. A direct native binary needs only its bin dir.
        dirs.append(os.path.dirname(os.path.dirname(real)))  # bin/x.js → pkg root
    return [d for d in dict.fromkeys(dirs) if d and os.path.isdir(d)]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SandboxMount:
    """A conditional mount from an MCP manifest's sandbox config."""
    host: str       # resolved host path
    sandbox: str    # mount point inside sandbox
    mode: str       # "ro" or "rw"


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


# Sandbox-internal destinations an MCP manifest mount must NEVER target.
# Conditional manifest mounts run LAST in build_command_prefix, so a later
# ``--ro-bind`` would shadow whatever already sits at the destination. Without
# this guard a malicious community manifest could overlay its own file onto the
# permission-gate hook (under ``.claude``) to disable tool gating, or shadow
# ``/etc``/``/proc``/the shared ``/config`` + ``/knowledge`` trees. The
# permission hook + per-session config/secrets live under ``.claude``/``.codex``.
_PROTECTED_MOUNT_DEST_ROOTS = (
    "/config", "/knowledge", "/etc", "/proc", "/sys", "/dev", "/run",
    "/tmp", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var", "/root",
    "/boot", "/home",
)
_PROTECTED_MOUNT_DEST_EXACT = frozenset({"/", "/workspace", "/users"})


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
    """Constructs bwrap command prefix from a SandboxConfig.

    Pure function: SandboxConfig -> list[str]. Does not spawn processes.
    """

    def __init__(self, cfg: SandboxConfig):
        self.cfg = cfg
        self._agent_dir = cfg.host_agents_dir / cfg.agent_name

    def build_command_prefix(self, inner_cmd: list[str]) -> list[str]:
        """Wrap inner_cmd with bwrap (+ the netns launcher when enabled)."""
        args = ["bwrap"]
        args.extend(self._namespace_flags())
        args.extend(self._cap_flags())
        args.extend(self._system_mounts())
        args.extend(self._claude_config_mount())
        args.extend(self._workspace_mounts())
        args.extend(self._mcp_mounts())
        args.extend(self._conditional_mcp_mounts())
        args.extend(["--chdir", self.get_cwd()])
        args.append("--")
        args.extend(inner_cmd)
        # Isolation is ALWAYS on. An empty forward set means the egress resolver
        # was never consulted (a build error — every session must come through
        # resolve_sandbox_config) — fail CLOSED rather than launch the agent
        # un-isolated OR netns-wrapped without the permission-hook port.
        if not self.cfg.net_forwards:
            raise RuntimeError(
                "sandbox network egress was not resolved (empty net_forwards) — "
                "refusing to launch a local agent. Sessions must be built via "
                "resolve_sandbox_config(); see core/sandbox/sandbox.py."
            )
        return self._netns_launcher_prefix() + args

    def _netns_active(self) -> bool:
        """Always true for a resolved session (isolation is mandatory).

        Kept as a single predicate for the uid-map-back + resolv-swap decisions;
        false only for a malformed config with no egress set, which
        build_command_prefix rejects outright.
        """
        return bool(self.cfg.net_forwards)

    def _netns_launcher_prefix(self) -> list[str]:
        """The oto-sandbox-net argv prepended to bwrap (pasta outside, bwrap inside).

        bwrap keeps `--share-net`: under the launcher it shares pasta's
        isolated netns, not the host's. Loopback forwards are spliced by pasta
        (`-T`); routable allow-hosts are carved /32·/128 routes; outbound rides
        NAT. The generated-resolv.conf check mirrors `_system_mounts` (same
        single signal — file existence).
        """
        prefix = [str(_NETNS_LAUNCHER), "--block-private"]
        for port in self.cfg.net_forwards:
            prefix.extend(["--forward", str(port)])
        for host in self.cfg.net_allow_hosts:
            prefix.extend(["--allow-host", str(host)])
        if netns_resolv_path().exists():
            prefix.extend(["--dns-forward", _NETNS_DNS_FORWARD_ADDR])
        prefix.append("--")
        return prefix

    def get_cwd(self) -> str:
        """Sandbox-internal CWD for this role."""
        username = self.cfg.username

        if not username:
            # Agent-scoped task
            return "/workspace"
        # All user roles (viewer / manager / admin)
        return f"/users/{username}"

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
        # Config dir lives inside the user or workspace dir (no separate mount)
        if self.cfg.username:
            cfg_dir = f"/users/{self.cfg.username}/{config_dir_name}"
        else:
            cfg_dir = f"/workspace/{config_dir_name}"

        env = {
            config_env_var: cfg_dir,
            # Restrict PATH to system dirs only (user home isn't mounted)
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "HOME": "/tmp",  # CLI needs a writable HOME for temp files
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

    def _namespace_flags(self) -> list[str]:
        flags = [
            "--unshare-pid",      # new PID namespace (can't see other processes)
            "--unshare-ipc",      # own SysV/POSIX-mq namespace — all local
                                  # sandboxes share the proxy uid, so a shared
                                  # IPC namespace would be a live cross-agent
                                  # shm/semaphore channel (/dev/shm is per-
                                  # sandbox tmpfs already; this closes shmget)
            "--unshare-uts",      # own hostname namespace (completeness)
            "--die-with-parent",  # kill sandbox if parent dies
            "--share-net",        # share network (hooks need localhost:8400;
                                  # under the netns launcher this is pasta's
                                  # isolated netns, not the host's)
        ]
        flags.extend(self._netns_uid_flags())
        return flags

    def _netns_uid_flags(self) -> list[str]:
        """Preserve the agent's host-native uid/gid when wrapped in pasta.

        Rootless pasta (proxy uid != 0, the host-native dev install) runs
        bwrap inside a user namespace that maps the proxy uid -> 0, so a plain
        bwrap hands the agent **uid 0** instead of today's proxy uid (verified
        on the 5.15 dev host: files still land host-owned either way, but the
        in-sandbox uid flips to root — a behavior delta that trips tools'
        run-as-root guards). Mapping back to the proxy's real uid/gid makes
        the in-sandbox identity byte-identical to the flag-off path; the only
        delta becomes the network policy.

        If the proxy runs as root (no default topology does — an operator
        override only), bwrap nests no user namespace and already hands the agent
        uid 0, so no map-back is needed and none is emitted. Empty when netns is
        inactive → legacy argv intact. (T2/cloud run the proxy as uid 1000, so
        they take the rootless map-back branch, identical to bare-metal.)
        """
        if not self._netns_active() or os.getuid() == 0:
            return []
        return [
            "--unshare-user",
            "--uid", str(os.getuid()),
            "--gid", str(os.getgid()),
        ]

    def _cap_flags(self) -> list[str]:
        """Drop ALL Linux capabilities from the sandboxed agent — unconditionally.

        The agent never needs a Linux capability (it runs the CLI + workspace
        tooling; nothing requires CAP_*). Dropping them is the single uniform
        invariant: the agent always holds zero capabilities, regardless of how
        the proxy was launched.

        It is load-bearing when the proxy runs as root (e.g. an operator who runs
        the image as root): there bwrap nests no user namespace, so without this
        the agent would inherit the container's full capability set — empirically
        CapEff=0xa82425fb (CAP_SYS_ADMIN, CAP_DAC_OVERRIDE, CAP_MKNOD, …), a large
        kernel attack surface for a sandbox/namespace breakout. `--cap-drop ALL`
        zeroes the effective + bounding set for the payload AFTER bwrap performs
        its own (privileged) mount/pivot_root setup, so the sandbox is still built
        but the agent holds no caps (verified: CapEff=0, workspace still writable
        via ownership, RO binds still enforced, mount() refused).

        On the non-root paths (bare-metal T1, and the containerised T2/cloud where
        the proxy runs as uid 1000) bwrap nests a user namespace where caps are
        already namespaced and powerless on the host — so this is a harmless,
        free belt-and-braces there (verified: rootless bwrap + --cap-drop ALL →
        uid unchanged, CapEff=0, python still runs).
        """
        return ["--cap-drop", "ALL"]

    def _system_mounts(self) -> list[str]:
        args: list[str] = []

        # Required RO binds
        for path in _SYSTEM_RO_BINDS:
            args.extend(["--ro-bind", path, path])

        # Optional RO binds (some paths don't exist on all systems)
        for path in _SYSTEM_RO_OPTIONAL:
            if os.path.exists(path):
                args.extend(["--ro-bind", path, path])

        # Individual system files
        netns_resolv = (
            netns_resolv_path() if self._netns_active() else None
        )
        for path in _SYSTEM_RO_FILES:
            # Stub-resolver netns mode: shadow the host's loopback resolv.conf
            # (127.0.0.53 — unreachable inside the netns) with the generated
            # one pointing at pasta's --dns-forward address. Same single signal
            # as the launcher's --dns-forward (the file's existence).
            if (path == "/etc/resolv.conf" and netns_resolv is not None
                    and netns_resolv.exists()):
                args.extend(["--ro-bind", str(netns_resolv), "/etc/resolv.conf"])
                continue
            if os.path.exists(path):
                args.extend(["--ro-bind", path, path])

        # Extra RO binds from layer config (e.g. Codex npm-global directory)
        for path in self.cfg.extra_ro_binds:
            if os.path.exists(path):
                args.extend(["--ro-bind", path, path])

        # Minimal /dev, /proc, /tmp
        args.extend(["--dev", "/dev"])
        args.extend(["--proc", "/proc"])
        args.extend(["--tmpfs", "/tmp"])

        return args

    def _claude_config_mount(self) -> list[str]:
        """No-op: .claude/ is inside the user or workspace dir (already mounted)."""
        return []

    def _workspace_mounts(self) -> list[str]:
        """Role-dependent workspace mounts (3-tier per-agent model).

        Mount table:

        | Role                  | /config | /knowledge | /workspace | /users/{u} |
        |-----------------------|---------|------------|------------|------------|
        | manager (= owner)     | RW      | RW         | RW         | RW*        |
        | editor                | (none)  | RO         | RW         | RW*        |
        | viewer                | (none)  | RO         | RO         | RW*        |
        | admin                 | RW      | RW         | RW         | RW*        |
        | agent-scope (no user) | (none)  | RO†        | RW         | (none)     |

        *RW* = the user dir's ROOT is RO; its known subdirs (workspace/,
        context/, .claude/, .codex/, .credentials/) stack RW on top, so
        stray root-level files are kernel-denied for every tool path.

        †RO with `knowledge/.credentials/` stacked RW on top (when it
        exists): agent-scope MCP credentials live there and MCP processes
        write-check + self-refresh tokens in place. Tool access to the
        subtree stays blocked at the application layer (path_policy).

        `/config/` is **owner-only** — editor + viewer don't see it at all.
        Config shapes agent BEHAVIOR (prompt, MCP wiring, auto-loaded
        context). That's owner curation, not workspace collaboration.

        Kernel-level enforcement is the single source of truth for file
        access — sidesteps Codex's hook bypass for non-Bash tools. The
        application-level path_policy is defense-in-depth that runs in the
        Claude Code hook only.
        """
        args: list[str] = []
        role = self.cfg.role
        username = self.cfg.username
        agent_dir_path = self._agent_dir
        agent_dir = str(agent_dir_path)
        # Anchor for symlink-refusing bind-source checks below. The agent
        # root itself is platform-owned (safe to resolve); everything BELOW
        # it that we bind from an agent-writable subtree must additionally
        # prove it carries no symlinked component (_verified_literal_path).
        agent_root_real = Path(os.path.realpath(agent_dir_path))

        # Defensive: bwrap --bind fails if source doesn't exist. The agent
        # template creation (agent_store.create_agent) now creates knowledge/
        # up front, but legacy agents won't have it until the one-shot
        # role_v2 migration runs. mkdir(exist_ok=True) is cheap insurance.
        (agent_dir_path / "knowledge").mkdir(parents=True, exist_ok=True)

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
        except Exception:
            pass  # never block a sandbox build on quota assignment

        lib_attachments = self.cfg.knowledge_libraries

        def _mirror_host(src: str, subdir: str) -> str:
            # bwrap --bind fails on a missing source, and under an RO parent
            # bind it cannot create the mountpoint either — host-side mkdir
            # is the same insurance as the knowledge/ mkdir above.
            # Symlink-refusing: the mirror chain lives under the RW-bindable
            # /knowledge, so an owner-tier agent could have replaced a
            # component with a symlink (detach/re-attach leaves plain dirs
            # behind). Verify BEFORE mkdir — mkdir(parents=True) through a
            # symlinked component would create dirs at the TARGET — and fail
            # the build loudly on tampering rather than bind the target.
            parts = ["knowledge", "shared", *Path(src).parts]
            if subdir:
                parts += Path(subdir).parts
            p = _verified_literal_path(agent_root_real, *parts)
            if p is None:
                raise RuntimeError(
                    f"Refusing sandbox build: knowledge mirror path "
                    f"knowledge/shared/{src}{'/' + subdir if subdir else ''} "
                    f"contains a symlinked component (possible tampering)"
                )
            p.mkdir(parents=True, exist_ok=True)
            if os.path.realpath(p) != str(p):
                raise RuntimeError(
                    f"Refusing sandbox build: knowledge mirror path {p} "
                    f"changed underneath the build (possible tampering)"
                )
            return str(p)

        def _mirror_dest(src: str, subdir: str) -> str:
            return (f"/knowledge/shared/{src}/{subdir}" if subdir
                    else f"/knowledge/shared/{src}")

        config_visible = self.cfg.config_visible
        if config_visible is None:
            # Not explicitly resolved (direct construction / pre-visibility-modes
            # caller): historical behavior — /config for an owner-tier role with
            # a real mount user (user-scope manager/admin).
            config_visible = role in ("manager", "admin") and bool(username)
        mount_shared = self.cfg.mount_shared

        if not username:
            # Agent-scope MOUNT — a service session (phone / task / trigger /
            # meeting) OR a Shared-only HUMAN chat (``mount_username==""`` for
            # both). Role-aware: a Shared-only viewer is read-only while a
            # manager curates. Service sessions force role=manager +
            # config_visible=False, so this stays byte-identical to the
            # pre-visibility-modes agent branch (/workspace RW + /knowledge RO).
            if role == "viewer":
                args.extend(["--ro-bind", f"{agent_dir}/workspace", "/workspace"])
            else:
                args.extend(["--bind", f"{agent_dir}/workspace", "/workspace"])
            if config_visible or self.cfg.knowledge_rw:
                # Owner-tier human in the agent scope (curates knowledge +
                # config) — or a manager-provenance task fire (knowledge_rw:
                # knowledge RW, /config deliberately NOT mounted).
                args.extend(["--bind", f"{agent_dir}/knowledge", "/knowledge"])
                if config_visible:
                    args.extend(["--bind", f"{agent_dir}/config", "/config"])
                # Read-only library mirrors stay kernel-RO under the RW
                # parent — one nested bind per library SUBTREE.
                for _src, _subdir, _writable in lib_attachments:
                    if not _writable:
                        args.extend(["--ro-bind", _mirror_host(_src, _subdir),
                                     _mirror_dest(_src, _subdir)])
            else:
                # Knowledge is universal + RO for non-owners / service sessions
                # (mirrors ride the RO parent — no nested binds needed).
                args.extend(["--ro-bind", f"{agent_dir}/knowledge", "/knowledge"])
                # Agent-scope MCP credentials live at knowledge/.credentials
                # (path_roles credentials_dir role) and MCP processes must
                # WRITE there: workspace-mcp write-checks the dir on boot and
                # self-refreshes tokens in place. RW child bind AFTER the RO
                # root (bwrap later-bind precedence) — fixed literal subtree,
                # never a manifest-supplied path. Existence-guarded: bwrap
                # cannot create a mountpoint under an RO parent, and the dir
                # exists iff credential_resolver materialized a bound account
                # (which runs before the sandbox is built).
                # Symlink-refusing: owner-tier sessions hold /knowledge RW,
                # so this path is agent-plantable across runs — a symlink
                # here would bind its TARGET read-write into this sandbox
                # (bwrap resolves bind sources). Refuse and boot without the
                # bind instead: the MCP shows up unconnected, nothing leaks.
                cred_dir = _verified_literal_path(
                    agent_root_real, "knowledge", ".credentials")
                if cred_dir is None:
                    logger.error(
                        "Refusing knowledge/.credentials bind for agent %s: "
                        "path contains a symlinked component (possible "
                        "tampering)", self.cfg.agent_name,
                    )
                elif cred_dir.is_dir():
                    args.extend(["--bind", str(cred_dir),
                                 "/knowledge/.credentials"])
            return args

        # User-scope MOUNT — the session has its own personal dir. The dir
        # ROOT is read-only with the known subdirs stacked RW on top: agents
        # kept scattering stray files next to workspace/ and context/ (the
        # dashboard file browser shows this root), and only the kernel stops
        # Codex native writes + Bash, which both bypass the permission hook.
        # Unknown future subdirs stay visible (RO) — a clean EROFS beats a
        # silent stray. Mirrored by the path-policy write allowlist
        # (auth/path_policy.py::_USER_DIR_WRITABLE_SUBDIRS).
        user_dir = agent_dir_path / "users" / username
        # Codex's own Linux sandbox (a nested bwrap) mounts read-only tmpfs
        # over <writable-root>/{.git,.agents,.codex} for every writable root,
        # cwd included — and bwrap must CREATE a missing mountpoint, which
        # this deliberately read-only root refuses ("Can't mkdir
        # /users/<u>/.git: Read-only file system"; the helper dies and codex
        # surfaces tool failures like view_image's "unable to locate image").
        # Pre-create the two codex doesn't make itself as EMPTY dirs — git
        # treats a bare .git dir as "not a git repository" and walks past;
        # .codex is session_config_dir's job and is RW-bound below.
        for sub in (".git", ".agents"):
            (user_dir / sub).mkdir(parents=True, exist_ok=True)
        args.extend(["--ro-bind", str(user_dir), f"/users/{username}"])
        for sub in ("workspace", "context"):
            (user_dir / sub).mkdir(parents=True, exist_ok=True)
            args.extend(["--bind", str(user_dir / sub), f"/users/{username}/{sub}"])
        # CLI state dirs (session's own is pre-created by the layer) + the
        # per-user MCP OAuth token dir (MCP processes refresh tokens in place).
        for sub in (".claude", ".codex", ".credentials"):
            if (user_dir / sub).is_dir():
                args.extend(["--bind", str(user_dir / sub), f"/users/{username}/{sub}"])

        # /config — owner-tier only (the agent's behavior layer). Hoisted out of
        # the role branch so Personal-only managers (no shared dirs) still get it.
        if config_visible:
            args.extend(["--bind", f"{agent_dir}/config", "/config"])

        # Shared workspace + knowledge — present only when the agent's mode
        # offers the agent scope. Personal-only (``mount_shared=False``) omits
        # BOTH: it is fully private, no shared collaboration surface.
        if mount_shared:
            if role in ("manager", "admin"):
                # Owner tier: knowledge RW (reference library) + workspace RW.
                args.extend(["--bind", f"{agent_dir}/knowledge", "/knowledge"])
                args.extend(["--bind", f"{agent_dir}/workspace", "/workspace"])
                # Read-only library mirrors stay kernel-RO under the RW
                # parent — one nested bind per library SUBTREE.
                for _src, _subdir, _writable in lib_attachments:
                    if not _writable:
                        args.extend(["--ro-bind", _mirror_host(_src, _subdir),
                                     _mirror_dest(_src, _subdir)])
            elif role == "editor":
                # Editor: workspace RW (collaboration), knowledge RO.
                args.extend(["--ro-bind", f"{agent_dir}/knowledge", "/knowledge"])
                args.extend(["--bind", f"{agent_dir}/workspace", "/workspace"])
            else:
                # Viewer (or unknown): both RO — SEE state + docs, mutate nothing.
                args.extend(["--ro-bind", f"{agent_dir}/knowledge", "/knowledge"])
                args.extend(["--ro-bind", f"{agent_dir}/workspace", "/workspace"])
        elif lib_attachments:
            # Personal-only: no shared /knowledge exists, but attached
            # libraries still mount — each mirror individually, ALWAYS
            # read-only (a personal-only agent has no knowledge-curation
            # surface, so the writable flag deliberately does not apply).
            # With no parent bind, bwrap creates the /knowledge/shared
            # chain inside the sandbox root itself.
            for _src, _subdir, _writable in lib_attachments:
                args.extend(["--ro-bind", _mirror_host(_src, _subdir),
                             _mirror_dest(_src, _subdir)])

        return args

    def _mcp_mounts(self) -> list[str]:
        """Mount this session's MCP dirs at the SAME absolute paths (RO).

        Identity binds keep all resolved paths in mcp-config.json valid inside
        the sandbox without rewriting. Only the dirs in ``cfg.mcp_dir_binds``
        (the session's assigned stdio MCPs + ``mcps/.uv-python``) are bound —
        never the whole mcps/ tree, so an agent can't read the code/config/data
        of MCPs it isn't assigned.

        The per-session MCP config file is NOT mounted from sessions/ —
        it's copied into the .claude/ dir by prepare_mcp_config_for_sandbox()
        to avoid exposing the sessions/ directory.
        """
        args: list[str] = []
        for d in self.cfg.mcp_dir_binds:
            if os.path.isdir(d):
                args.extend(["--ro-bind", d, d])
        return args

    def _conditional_mcp_mounts(self) -> list[str]:
        """Additional mounts declared in an MCP manifest's ``sandbox.mounts``.

        Both ends are constrained against a malicious community manifest:
          * HOST (source) must resolve inside THIS agent's own tree or the
            mcps/ install tree — never the platform root (which holds
            ``config.env`` + ``sessions/`` OAuth material) nor another agent's
            tree.
          * DESTINATION must not overlay the permission-gate hook / per-session
            config (``.claude``/``.codex``), the shared ``/config`` +
            ``/knowledge`` trees, or any system mount (see
            ``_is_safe_mcp_mount_dest``) — these mounts run last, so a bind
            there would shadow the real one.
        System dirs the sandbox legitimately needs are handled by
        ``_system_mounts``, not here.
        """
        allowed_roots = []
        for root in (self._agent_dir, self.cfg.host_mcps_dir):
            try:
                allowed_roots.append(Path(root).resolve())
            except (OSError, ValueError):
                continue
        args: list[str] = []
        for mount in self.cfg.mcp_sandbox_mounts:
            if not os.path.exists(mount.host):
                continue
            host_resolved = Path(mount.host).resolve()
            if not any(host_resolved.is_relative_to(r) for r in allowed_roots):
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
            target = str(host_resolved)
            if mount.mode == "rw":
                args.extend(["--bind", target, mount.sandbox])
            else:
                args.extend(["--ro-bind", target, mount.sandbox])
        return args


# Per-session config-directory setup lives in session_config_dir.py; the hook
# helpers + ensure_persistent_*/prepare_mcp builders are re-exported here so
# existing `from core.sandbox.sandbox import ensure_persistent_claude_dir` (and the many
# call-time local imports across the codebase) keep working unchanged.
from core.sandbox.session_config_dir import (  # noqa: F401
    _copy_hook_lf,
    _DISALLOWED_BUILTIN_TOOLS,
    _build_sandbox_cli_settings,
    ensure_persistent_claude_dir,
    ensure_persistent_codex_dir,
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
        d = str(mcps_dir / m.group(1) / m.group(2))
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            binds.append(d)
    if binds:
        uv_python = mcps_dir / ".uv-python"
        if uv_python.is_dir():
            binds.append(str(uv_python))
    return binds


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
) -> SandboxConfig:
    """Build a SandboxConfig from session context.

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
            net_forwards, _allow = mcp_registry.resolve_sandbox_egress(
                agent_name, user_sub=user_sub, extra_targets=extra_egress_targets,
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
        from storage import db_knowledge_libraries
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
    )
