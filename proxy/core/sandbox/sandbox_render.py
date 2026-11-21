"""One render of a session's sandbox command, from a host-neutral description.

The proxy resolves a session's sandbox (its role rows, its tools, its
network routes: ``core/sandbox/sandbox.py``) into a ``SandboxDescription``
that names no path, user id or port of the host it was resolved on. ``render``
turns it into the launcher and bwrap argv on the host that will run it, from
that host's own ``RenderHost``: the proxy renders its own sessions with its
own values, and a compute machine renders the same description with its
agents root, its tools root, its empty mask folder, its uid, its tunnel port
and its own resolver swap. There is no second builder of the argv anywhere.

Stdlib only: the module is vendored into the satellite
(``satellite/_vendored/sandbox_render.py``, ``scripts/sync-satellite-code.sh``)
beside the launcher it names, and runs on Python 3.10.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import stat
import subprocess
from dataclasses import asdict, dataclass, fields
from pathlib import Path

#: A bind source's root: the agent's resolved folder (``agents``), the tools
#: folder (``mcps``), the renderer's empty mask folder (``empty``), the host's
#: null device (``null``: a mask over a file name, whose mountpoint is an empty
#: file), or an absolute path of the resolving host (``abs``: an app server's
#: table, which only the proxy renders and a machine refuses).
ROOT_AGENT = "agents"
ROOT_MCPS = "mcps"
ROOT_EMPTY = "empty"
ROOT_NULL = "null"
ROOT_ABS = "abs"
#: The forward entry that means the proxy's port: the renderer splices it
#: onto the port its host names (the proxy's internal listener, a machine's
#: tunnel), and every other entry is a literal port spec.
PROXY_FORWARD = "proxy"
#: pasta's in-namespace resolver address when the host's resolver is a
#: loopback stub (carved out of the link-local blackhole by the shim).
DNS_FORWARD_ADDR = "169.254.1.1"
#: Where a session's SSH agent socket is bound inside the sandbox: under the
#: private ``/tmp`` the system mounts make, so bwrap creates the file node.
SSH_AGENT_SOCKET = "/tmp/oto-ssh-agent.sock"
#: Where a host that serves its own credential exit binds its trust bundle
#: inside the sandbox: what a compute session's trust variables name.
EXIT_BUNDLE_PATH = "/tmp/oto-exit-ca.pem"
#: The name of a session's socket file in its host's socket folder.
_SSH_AGENT_NAME_RE = re.compile(r"^[0-9a-f]{12}\.sock$")

#: A description's kind: a session's (the default, left out of the wire form)
#: or one step of a tool's build (``build_description``), which only the host
#: that made it renders: a build's description never travels.
KIND_SESSION = "session"
KIND_BUILD = "build"
#: The OOM score a build's processes and a compute session's run at: above a
#: compute machine's satellite (250) and the proxy (0), raised only.
BUILD_OOM_SCORE_ADJ = 500
#: What each step of a build may write in its tool's folder (the installer's
#: ``STEPS``); the rest of the folder is read-only to it.
BUILD_OUTPUTS = {
    "venv": ("venv",), "pip": ("venv",), "npm": ("node_modules", "package-lock.json"),
    "patch": ("node_modules",), "rebuild": ("node_modules",),
}
#: The outputs above that are files, written in place (the others are folders).
BUILD_OUTPUT_FILES = frozenset({"package-lock.json"})
#: A tool's folders that hold its keys and its data, never shown to a build.
BUILD_MASKED = ("keys", "config", "screenshots")
#: The environment names that say where a build's requests go: a proxy, an
#: index, an interpreter mirror (the installer's allowlist).
BUILD_EGRESS_NAMES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_DEFAULT_INDEX",
    "UV_INDEX", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "NPM_CONFIG_REGISTRY", "UV_PYTHON_INSTALL_MIRROR",
    "UV_PYPY_INSTALL_MIRROR",
)
#: The environment names that say how a build's requests are made (which
#: hosts skip the proxy, which are trusted, a timeout): the rest of the
#: installer's allowlist that an install on another host must carry. They
#: name no endpoint, so nothing is carved for them, and never a cache
#: folder, which is each host's own.
BUILD_SETTING_NAMES = (
    "NO_PROXY", "PIP_TRUSTED_HOST", "NPM_CONFIG_STRICT_SSL", "UV_NATIVE_TLS", "UV_HTTP_TIMEOUT",
    "UV_OFFLINE", "UV_PYTHON_DOWNLOADS",
)
#: The environment names that name a trust file a build must read.
BUILD_TRUST_NAMES = ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
                     "NODE_EXTRA_CA_CERTS", "NPM_CONFIG_CAFILE", "GIT_SSL_CAINFO")
#: The one of them that names a folder, and the names of the files in it a
#: TLS library looks up (OpenSSL's subject hash and a counter).
BUILD_TRUST_DIR_NAME = "SSL_CERT_DIR"
BUILD_TRUST_DIR_FILE = r"[0-9a-f]{8}\.[0-9]{1,3}"

#: The file the proxy keeps at its agents root: a satellite that is not a
#: compute machine refuses to start on a folder that carries it (its sweeps
#: and its sync would purge and rewrite the platform's files).
PLATFORM_MARKER = ".otodock-platform"

# Read-only system binds every sandbox gets: required, existence-checked, and
# single files (checked one by one).
_SYSTEM_RO_BINDS = [
    "/usr",
    "/bin",
    "/lib",
    "/sbin",
]
_SYSTEM_RO_OPTIONAL = [
    "/lib64",
    "/lib32",
    "/snap/bin",   # snap-installed CLIs (gh, kubectl, ...) — a no-op elsewhere
]
_SYSTEM_RO_FILES = [
    "/etc/resolv.conf",
    "/etc/ssl",
    "/etc/hosts",
    "/etc/passwd",
    "/etc/group",
    "/etc/nsswitch.conf",
    # The host's git configuration: a session's own git entries reset its
    # helper in favour of the credential helper.
    "/etc/gitconfig",
]


class RenderRefused(RuntimeError):
    """The description names a source this host may not bind, or no route
    at all (the callers' contract: a ``RuntimeError`` at the spawn)."""


@dataclass(frozen=True)
class Bind:
    """One bind of the description: ``root`` and ``rel`` name the source on
    whatever host renders it, ``sandbox`` is the mount point inside."""
    root: str
    rel: str
    sandbox: str
    rw: bool


@dataclass(frozen=True)
class SandboxDescription:
    """What the proxy resolved for one session, with nothing of its host in
    it. ``binds`` is the role table with the hook-file binds, in bwrap's
    order; ``mcp_binds`` the session's tool folders (identity binds, read
    only, relative to the tools root, bound at ``mcps_root`` inside: the
    MCP config names those paths); ``extra_binds`` the manifest mounts the
    proxy checked. ``forwards`` holds ``PROXY_FORWARD`` for the proxy port
    and literal port specs beside it. ``beneath`` says the role table's
    sources are held by the shim (false for an app server's table).
    ``tmp_size_mb`` is the proxy's cap for the private ``/tmp`` (0: none).
    ``exit_port`` is the port the session dials inside its namespace for the
    credential exit (0: none; a host that serves its own exit binds its
    trust bundle at ``EXIT_BUNDLE_PATH`` then); ``ssh_agent`` the file name
    of the session's SSH agent socket in the rendering host's socket folder
    ("": none). ``home_masks`` is the ``own`` folder of every engine folder the
    session sees, where the sessions' homes are (``session_home``), each
    masked empty and read-only; ``home`` the session's own home, bound back in
    writable (``home_args``). All four are left out of the wire form at their
    default, so a description without them is byte for byte the one an older
    render knows."""
    agent: str
    binds: tuple[Bind, ...]
    mcp_binds: tuple[str, ...]
    mcps_root: str
    extra_binds: tuple[Bind, ...]
    cwd: str
    proxy_port: int
    forwards: tuple[str, ...]
    allow_hosts: tuple[str, ...] = ()
    hosted: bool = False
    inbound: str = ""
    egress_deny: bool = False
    beneath: bool = True
    tmp_size_mb: int = 0
    exit_port: int = 0
    ssh_agent: str = ""
    kind: str = KIND_SESSION
    home_masks: tuple[Bind, ...] = ()
    home: Bind | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if not self.exit_port:
            del d["exit_port"]
        if not self.ssh_agent:
            del d["ssh_agent"]
        if not self.home_masks:
            del d["home_masks"]
        if self.home is None:
            del d["home"]
        if self.kind == KIND_SESSION:
            del d["kind"]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> SandboxDescription:
        """The description as the wire carried it. A field this module does
        not know is refused, never dropped: a proxy that adds a mask or a
        refusal to the description must not get a sandbox without it from
        a machine running an older render (the upgrade is rolling)."""
        unknown = set(d) - {f.name for f in fields(cls)}
        if unknown:
            raise RenderRefused(f"the sandbox description names fields this render does not know: "
                                f"{', '.join(sorted(unknown))}")
        if d.get("kind", KIND_SESSION) != KIND_SESSION:
            raise RenderRefused("a description that is not a session's is rendered only where it was made")
        binds = tuple(Bind(**b) for b in d.get("binds", ()))
        extra = tuple(Bind(**b) for b in d.get("extra_binds", ()))
        return cls(
            agent=str(d["agent"]), binds=binds,
            mcp_binds=tuple(str(x) for x in d.get("mcp_binds", ())),
            mcps_root=str(d.get("mcps_root", "")), extra_binds=extra,
            cwd=str(d["cwd"]), proxy_port=int(d["proxy_port"]),
            forwards=tuple(str(x) for x in d.get("forwards", ())),
            allow_hosts=tuple(str(x) for x in d.get("allow_hosts", ())),
            hosted=bool(d.get("hosted", False)), inbound=str(d.get("inbound", "")),
            egress_deny=bool(d.get("egress_deny", False)), beneath=bool(d.get("beneath", True)),
            tmp_size_mb=int(d.get("tmp_size_mb", 0) or 0),
            exit_port=int(d.get("exit_port", 0) or 0), ssh_agent=str(d.get("ssh_agent", "")),
            home_masks=tuple(Bind(**b) for b in d.get("home_masks", ())),
            home=Bind(**d["home"]) if d.get("home") else None,
        )


@dataclass(frozen=True)
class RenderHost:
    """The host that renders: where its agents and tools are, its empty mask
    folder, the launcher it runs, its uid and gid, the port the proxy port is
    spliced onto (0: the proxy port itself), its generated resolver file when
    its own resolver is a loopback stub (``""``: none), the effective
    ``/tmp`` cap it can enforce (0: no ``--size``), the read-only binds that
    make its engine binary runnable, whether it renders an ``abs`` root
    (the proxy alone), the port the description's exit port is spliced onto
    (0: the exit port itself), the trust bundle of the exit this host serves
    itself, bound read-only at ``EXIT_BUNDLE_PATH`` ("": the session's config
    dir carries the platform's bundle, as a local session's does) and the
    folder its SSH agent sockets live in ("": this host serves none)."""
    agents_root: str
    mcps_root: str
    empty_dir: str
    launcher: str
    uid: int
    gid: int
    proxy_target_port: int = 0
    resolv_conf: str = ""
    tmp_cap_mb: int = 0
    extra_ro_binds: tuple[str, ...] = ()
    allow_abs: bool = False
    exit_target_port: int = 0
    exit_bundle: str = ""
    ssh_agent_dir: str = ""


def effective_tmp_cap_mb(configured_mb: int, has_size: bool, mem_total_mb: int) -> int:
    """The cap a host enforces on a sandbox's private ``/tmp``: the configured
    size bounded by half the host's RAM (the kernel's own tmpfs default, so
    the cap never loosens what an uncapped mount would get), 0 when the
    configuration disables it or the host's bubblewrap has no ``--size``."""
    if configured_mb <= 0 or not has_size:
        return 0
    half_ram = mem_total_mb // 2
    return min(configured_mb, half_ram) if half_ram > 0 else configured_mb


def probe_bwrap_size() -> bool:
    """Whether this host's bubblewrap accepts ``--size``."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        return False
    try:
        out = subprocess.run([bwrap, "--help"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return "--size" in (out.stdout or "") + (out.stderr or "")


def mem_total_mb() -> int:
    """Host RAM in MB from ``/proc/meminfo`` (0 when unreadable). In a
    container this is the host's figure, not the cgroup limit: a bound."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def host_resolv_has_loopback_ns(resolv_path: str = "/etc/resolv.conf") -> bool:
    """Whether a nameserver of the host's resolver file is a loopback address
    (systemd-resolved's stub, Docker's embedded resolver): unreachable inside
    the namespace, so the renderer swaps in a file naming pasta's forwarder.
    One loopback entry is enough: glibc tries the servers in order."""
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


def _npm_package_of(exe: str) -> str:
    """The npm package a globally installed CLI comes from, read from where
    its binary resolves (``…/node_modules/<pkg>/…`` or
    ``…/node_modules/@scope/<pkg>/…``); "" for a binary npm did not install."""
    parts = Path(os.path.realpath(exe)).parts
    if "node_modules" not in parts:
        return ""
    i = len(parts) - 1 - parts[::-1].index("node_modules")
    rest = parts[i + 1:]
    if not rest:
        return ""
    if rest[0].startswith("@") and len(rest) > 1:
        return f"{rest[0]}/{rest[1]}"
    return rest[0]


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


def agent_root(host: RenderHost, agent: str) -> str:
    """The agent's folder on this host, resolved once: the folder itself is
    platform-owned and never bound, so a link there (a folder moved to
    another disk) is the operator's and is followed here, as the proxy's
    builder does."""
    return os.path.realpath(os.path.join(host.agents_root, agent))


def source_of(bind: Bind, host: RenderHost, root: str) -> str:
    """Where ``bind``'s source is on this host."""
    if bind.root == ROOT_AGENT:
        return os.path.join(root, bind.rel) if bind.rel else root
    if bind.root == ROOT_MCPS:
        return os.path.join(host.mcps_root, bind.rel)
    if bind.root == ROOT_EMPTY:
        return host.empty_dir
    if bind.root == ROOT_NULL:
        return os.devnull
    if bind.root == ROOT_ABS:
        if not host.allow_abs:
            raise RenderRefused(f"this host binds no absolute source: {bind.rel}")
        return bind.rel
    raise RenderRefused(f"unknown bind root {bind.root!r}")


def ssh_agent_source(desc: SandboxDescription, host: RenderHost) -> str:
    """The session's SSH agent socket on this host: its file name under the
    host's socket folder, refused when the host serves none, the name is not
    a session's, or the socket is not there (a session told it has an agent
    must have it; bwrap would refuse a missing source anyway)."""
    if not host.ssh_agent_dir:
        raise RenderRefused("this host serves no SSH agent socket")
    if not _SSH_AGENT_NAME_RE.match(desc.ssh_agent):
        raise RenderRefused(f"the SSH agent socket name is not a session's: {desc.ssh_agent!r}")
    path = os.path.join(host.ssh_agent_dir, desc.ssh_agent)
    if not os.path.exists(path):
        raise RenderRefused(f"the SSH agent socket is not there: {path}")
    return path


def exit_bundle_source(desc: SandboxDescription, host: RenderHost) -> str:
    """The trust bundle a host that serves its own exit binds for a session
    whose description names the exit: refused when the host names none (its
    exit is not up: a splice onto nothing and a missing trust file would
    follow) or the file is not there."""
    if not host.exit_bundle:
        raise RenderRefused("this host serves no credential exit for a description that names one")
    if not os.path.isfile(host.exit_bundle):
        raise RenderRefused(f"the credential exit's trust bundle is not there: {host.exit_bundle}")
    return host.exit_bundle


def launcher_prefix(desc: SandboxDescription, host: RenderHost, root: str) -> list[str]:
    """The ``oto-sandbox-net`` argv ahead of bwrap: the held-bind root, the
    blackholes, the forwards with the proxy port spliced onto the host's
    port, the carves, an app's inbound splice and closed egress, and the
    resolver forward when the host swaps its resolver file."""
    prefix = [host.launcher]
    if desc.beneath:
        prefix.extend(["--beneath", root])
    prefix.append("--block-private")
    if desc.hosted:
        prefix.append("--hosted")
    for spec in desc.forwards:
        if spec == PROXY_FORWARD:
            spec = (f"{desc.proxy_port}:{host.proxy_target_port}" if host.proxy_target_port
                    else str(desc.proxy_port))
        prefix.extend(["--forward", spec])
    if desc.exit_port:
        # The credential exit: one more loopback splice, onto the host's
        # port where the exit is served elsewhere.
        prefix.extend(["--forward", (f"{desc.exit_port}:{host.exit_target_port}"
                                     if host.exit_target_port else str(desc.exit_port))])
    for addr in desc.allow_hosts:
        prefix.extend(["--allow-host", addr])
    if desc.inbound:
        prefix.extend(["--inbound", desc.inbound])
    if desc.egress_deny:
        prefix.append("--egress-deny")
    if host.resolv_conf and os.path.exists(host.resolv_conf):
        prefix.extend(["--dns-forward", DNS_FORWARD_ADDR])
    prefix.append("--")
    return prefix


def system_mounts(host: RenderHost) -> list[str]:
    """The read-only system binds, this host's resolver swap, its extra
    binds, a fresh ``/dev`` and ``/proc`` and the capped private ``/tmp``."""
    args: list[str] = []
    for path in _SYSTEM_RO_BINDS:
        args.extend(["--ro-bind", path, path])
    for path in _SYSTEM_RO_OPTIONAL:
        if os.path.exists(path):
            args.extend(["--ro-bind", path, path])
    swap = host.resolv_conf if host.resolv_conf and os.path.exists(host.resolv_conf) else ""
    for path in _SYSTEM_RO_FILES:
        if path == "/etc/resolv.conf" and swap:
            args.extend(["--ro-bind", swap, "/etc/resolv.conf"])
            continue
        if os.path.exists(path):
            args.extend(["--ro-bind", path, path])
    for path in host.extra_ro_binds:
        if os.path.exists(path):
            args.extend(["--ro-bind", path, path])
    args.extend(["--dev", "/dev"])
    args.extend(["--proc", "/proc"])
    if host.tmp_cap_mb > 0:
        args.extend(["--size", str(host.tmp_cap_mb * 1024 * 1024)])
    args.extend(["--tmpfs", "/tmp"])
    return args


def _leaves(rel: str) -> bool:
    """Whether a bind's ``rel`` leaves its root: absolute, or a parent step."""
    return rel.startswith("/") or ".." in rel.split("/")


def check_folders(desc: SandboxDescription) -> None:
    """A session description's folders, checked before anything is bound:
    its agent is one folder name (empty only for a description that binds
    nothing of the agents' root, as the host check's probe), each tool
    folder is plain names below the tools root, and no bind of the agent's
    or the tools' root leaves it. The proxy's builder makes none of these;
    the render does not depend on it."""
    binds = (*desc.binds, *desc.extra_binds, *desc.home_masks, *((desc.home,) if desc.home else ()))
    if "/" in desc.agent or (desc.agent and not desc.agent.strip(".")) or (
            not desc.agent and any(b.root == ROOT_AGENT for b in binds)):
        raise RenderRefused("this session's description names no agent folder")
    for rel in desc.mcp_binds:
        if rel.startswith("/") or not all(part.strip(".") for part in rel.split("/")):
            raise RenderRefused("a tool folder in this session's description is not a plain folder name")
    if any(b.root in (ROOT_AGENT, ROOT_MCPS) and _leaves(b.rel) for b in binds):
        raise RenderRefused("a folder in this session's description leaves its root")


def render(desc: SandboxDescription, host: RenderHost, inner: list[str]) -> list[str]:
    """The whole command: the launcher prefix, then bwrap with the sessions'
    namespace flags, no capability, the system mounts, the role table, the
    tool folders, the manifest mounts, the working directory and ``inner``.
    An empty forward set is refused for a session: a session without the
    proxy port was never resolved, and never runs. A build reaches no port
    of its host, holds every source below the tools root and is rendered
    only by a host that renders its own absolute sources (it made it)."""
    if desc.kind == KIND_BUILD:
        if not host.allow_abs or desc.exit_port or desc.ssh_agent:
            raise RenderRefused("a build's description is rendered only by the host that made it")
        root = os.path.realpath(host.mcps_root)
    elif desc.kind != KIND_SESSION:
        raise RenderRefused(f"unknown description kind {desc.kind!r}")
    elif not desc.forwards:
        raise RenderRefused(
            "sandbox network egress was not resolved (empty net_forwards) — "
            "refusing to launch a local agent. Sessions must be built via "
            "resolve_sandbox_config(); see core/sandbox/sandbox.py."
        )
    else:
        check_folders(desc)
        root = agent_root(host, desc.agent)
    args = ["bwrap", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--die-with-parent",
            "--share-net"]
    if host.uid != 0:
        # Rootless pasta nests a user namespace that would hand the agent uid
        # 0: map the host's own uid and gid back.
        args.extend(["--unshare-user", "--uid", str(host.uid), "--gid", str(host.gid)])
    args.extend(["--cap-drop", "ALL"])
    args.extend(system_mounts(host))
    if desc.ssh_agent:
        args.extend(["--bind", ssh_agent_source(desc, host), SSH_AGENT_SOCKET])
    if desc.exit_port and (host.exit_bundle or not host.allow_abs):
        # A host that serves its own exit (a compute machine) binds its
        # bundle; the proxy's render leaves the session's config dir to
        # carry the platform's.
        args.extend(["--ro-bind", exit_bundle_source(desc, host), EXIT_BUNDLE_PATH])
    for b in desc.binds:
        args.extend(["--bind" if b.rw else "--ro-bind", source_of(b, host, root), b.sandbox])
    args.extend(home_args(desc, host, root))
    for rel in desc.mcp_binds:
        src = os.path.join(host.mcps_root, rel)
        if os.path.isdir(src):
            args.extend(["--ro-bind", src, os.path.join(desc.mcps_root, rel)])
    for b in desc.extra_binds:
        src = source_of(b, host, root)
        if not os.path.exists(src):
            continue
        args.extend(["--bind" if b.rw else "--ro-bind", src, b.sandbox])
    args.extend(["--chdir", desc.cwd, "--"])
    args.extend(inner)
    return launcher_prefix(desc, host, root) + args


#: The files of a session's home that name its hooks: bound read-only over
#: themselves, as the scope's own are, so the session cannot rewrite them.
HOME_RO_FILES = ("settings.json", "hooks.json")
# A store's own sign-in, by its engine folder's name, written by a session
# without a home (a judge, Codex, one from before the upgrade), maybe for
# another account: a session that runs from a home sees it empty, whenever
# it is written. The platform's writer writes it in place, so the mount
# made over the empty file at the start holds over what comes later.
STORE_SIGN_IN_FILES = {".claude": ".credentials.json", ".codex": "auth.json"}


def home_args(desc: SandboxDescription, host: RenderHost, root: str) -> list[str]:
    """The sessions' homes (the platform's ``core/sandbox/session_home.py``):
    a memory folder over the ``own`` folder of every engine folder the
    session sees, so no other session's home (its sign-in) is there, then
    this session's own home bound back in writable with its hook-naming files
    read-only, then each memory folder read-only. A mask whose engine folder
    this host lacks hides nothing and is left out; its ``own`` is made
    (0700) when absent, and a link at any of these names is refused."""
    args: list[str] = []
    masked: list[str] = []
    for m in desc.home_masks:
        src = source_of(m, host, root)
        if not os.path.isdir(os.path.dirname(src)) or os.path.islink(os.path.dirname(src)):
            continue
        with contextlib.suppress(FileExistsError):
            os.mkdir(src, 0o700)
        if os.path.islink(src) or not os.path.isdir(src):
            raise RenderRefused(f"a session homes folder is not a folder: {src}")
        args.extend(["--tmpfs", m.sandbox])
        masked.append(m.sandbox)
    if desc.home is not None:
        src = source_of(desc.home, host, root)
        if os.path.islink(src) or not os.path.isdir(src):
            raise RenderRefused(f"the session's home is not a folder: {src}")
        args.extend(["--bind", src, desc.home.sandbox])
        for name in HOME_RO_FILES:
            f = os.path.join(src, name)
            if os.path.isfile(f) and not os.path.islink(f):
                args.extend(["--ro-bind", f, f"{desc.home.sandbox}/{name}"])
        for m in desc.home_masks:
            store = os.path.dirname(source_of(m, host, root))
            name = STORE_SIGN_IN_FILES.get(os.path.basename(store), "")
            if name and m.sandbox in masked:
                _sign_in_mount_point(os.path.join(store, name))
                args.extend(["--ro-bind", os.devnull, f"{os.path.dirname(m.sandbox)}/{name}"])
    for sandbox in masked:
        args.extend(["--remount-ro", sandbox])
    return args


def _sign_in_mount_point(path: str) -> None:
    """The store's sign-in as a mount point: an empty file made (0600, no
    link followed) when there is none, an existing regular file left as it
    is, anything else at the name refused."""
    with contextlib.suppress(FileExistsError):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    if os.path.islink(path) or not os.path.isfile(path):
        raise RenderRefused(f"a store's sign-in is not a file: {path}")


def oom_raise(cmd: list[str], score: int = BUILD_OOM_SCORE_ADJ) -> list[str]:
    """``cmd`` run behind the raise of its own OOM score: the shell ``exec``s
    it, so the pid and the argv are the command's own; never lowered."""
    snippet = (f'{{ read -r v </proc/self/oom_score_adj && [ "$v" -lt {score} ] '
               f'&& echo {score} >/proc/self/oom_score_adj; }} 2>/dev/null; exec "$@"')
    return ["/bin/sh", "-c", snippet, "oto-session", *cmd]


def build_description(tool_dir: str, step: str, *, mcps_root: str, scratch: str,
                      interpreter_dirs: tuple[str, ...], own_interpreter_dir: str,
                      data_dirs: tuple[str, ...] = (), ro_paths: tuple[str, ...] = (),
                      forwards: tuple[str, ...] = (), allow_hosts: tuple[str, ...] = (),
                      hosted: bool = False, tmp_size_mb: int = 0) -> SandboxDescription:
    """One step of a tool's build: the tool's folder read-only, the step's
    outputs (``BUILD_OUTPUTS``) and its scratch folder writable, the folders
    holding its keys and data masked, the interpreter folders read-only but
    its own in a ``venv`` step (the one step that fetches an interpreter and
    runs no package code), ``ro_paths`` (a CLI outside the system folders, a
    trust file) read-only. Every source must exist: the caller makes the
    outputs first. A ``venv`` step starts in its scratch folder, so no file of
    the package chooses its interpreter."""
    if step not in BUILD_OUTPUTS:
        raise RenderRefused(f"unknown build step {step!r}")
    binds = [Bind(ROOT_ABS, tool_dir, tool_dir, False)]
    for name in (*BUILD_MASKED, *data_dirs):
        path = os.path.join(tool_dir, name)
        if os.path.isdir(path) and not os.path.islink(path):
            binds.append(Bind(ROOT_EMPTY, "", path, False))
    for name in BUILD_OUTPUTS[step]:
        binds.append(Bind(ROOT_ABS, os.path.join(tool_dir, name), os.path.join(tool_dir, name), True))
    for path in interpreter_dirs:
        if os.path.isdir(path):
            binds.append(Bind(ROOT_ABS, path, path, step == "venv" and path == own_interpreter_dir))
    binds.append(Bind(ROOT_ABS, scratch, scratch, True))
    binds.extend(Bind(ROOT_ABS, path, path, False) for path in ro_paths if os.path.exists(path))
    return SandboxDescription(
        agent="", binds=tuple(binds), mcp_binds=(), mcps_root=mcps_root, extra_binds=(),
        cwd=scratch if step == "venv" else tool_dir, proxy_port=0, forwards=forwards,
        allow_hosts=allow_hosts, hosted=hosted, beneath=True, tmp_size_mb=tmp_size_mb,
        kind=KIND_BUILD,
    )


#: The names a step's own scratch folder answers, whatever its host's
#: environment says (compared in upper case, as the installer's allowlist is).
_BUILD_SCRATCH_NAMES = frozenset({
    "HOME", "TMPDIR", "TMP", "TEMP", "UV_CACHE_DIR", "PIP_CACHE_DIR", "NPM_CONFIG_CACHE",
    "XDG_CACHE_HOME", "PWD",
})
#: Folders a program in them needs no bind for: the system binds.
_BUILD_SYSTEM_ROOTS = ("/usr/", "/bin/", "/sbin/", "/lib/", "/lib64/", "/etc/ssl/")


def prepare_build_step(tool_dir: str, step: str, scratch: str, env: dict[str, str]) -> dict[str, str]:
    """Make the step's outputs (so each bind has its source: a folder, or the
    lock file npm writes in place) and its scratch folders, and answer
    ``env`` with the step's own HOME, temporary folder and caches: a fresh
    download cache for every step, shared with no other build."""
    for name in BUILD_OUTPUTS[step]:
        out = os.path.join(tool_dir, name)
        if name in BUILD_OUTPUT_FILES:
            if not os.path.lexists(out):
                open(out, "a").close()
        else:
            os.makedirs(out, exist_ok=True)
    home, tmp, cache = (os.path.join(scratch, n) for n in ("home", "tmp", "cache"))
    for folder in (home, tmp, cache):
        os.makedirs(folder, exist_ok=True)
    out_env = {k: v for k, v in env.items() if k.upper() not in _BUILD_SCRATCH_NAMES}
    out_env.update({
        "HOME": home, "TMPDIR": tmp, "TMP": tmp, "TEMP": tmp,
        "UV_CACHE_DIR": os.path.join(cache, "uv"), "PIP_CACHE_DIR": os.path.join(cache, "pip"),
        "NPM_CONFIG_CACHE": os.path.join(cache, "npm"), "XDG_CACHE_HOME": cache,
    })
    return out_env


def build_data_dirs(tool_dir: str) -> tuple[str, ...]:
    """The first segment of each folder the tool's manifest declares as its
    data (``data_dirs``), masked from its build with its keys."""
    import json
    try:
        with open(os.path.join(tool_dir, "manifest.json"), encoding="utf-8") as fh:
            declared = json.load(fh).get("data_dirs") or {}
    except (OSError, ValueError, AttributeError):
        return ()
    if not isinstance(declared, dict):
        return ()
    names = {str(v).strip("/").split("/")[0] for v in declared.values()}
    root = os.path.normpath(tool_dir)
    # A folder of the tool itself: never the tool's own folder or its parent.
    return tuple(sorted(n for n in names
                        if n and os.path.dirname(os.path.normpath(os.path.join(root, n))) == root))


def _outside_system(path: str) -> bool:
    return not any(path == root.rstrip("/") or path.startswith(root) for root in _BUILD_SYSTEM_ROOTS)


def build_ro_paths(cmd: list[str], step: str, env: dict[str, str]) -> tuple[str, ...]:
    """What a step reads outside the system folders: the program it starts
    (uv, npm with node, git, an interpreter) where it is installed in a home
    folder, and the trust files its environment names."""
    paths: list[str] = []
    programs = [cmd[0]] + (["node"] if step in ("npm", "rebuild") else [])
    for program in programs:
        found = program if os.path.isabs(program) else shutil.which(program, path=env.get("PATH"))
        if found and _outside_system(os.path.realpath(found)):
            paths.extend(cli_install_ro_binds(found))
    trust = {n.upper() for n in BUILD_TRUST_NAMES}
    for key, value in env.items():
        if key.upper() in trust and value and os.path.exists(value) and _outside_system(os.path.realpath(value)):
            paths.append(value)
    return tuple(dict.fromkeys(paths))


_DEFAULT_PORTS = {"https": 443, "http": 80}


def build_endpoints(env: dict[str, str]) -> list[tuple[str, int]]:
    """The hosts and ports a build's requests go to by its environment (a
    proxy, an index, a mirror; ``BUILD_EGRESS_NAMES`` in any case), for the
    host to carve. An ``index`` entry of ``UV_INDEX`` may be ``name=url``."""
    from urllib.parse import urlsplit
    found: list[tuple[str, int]] = []
    wanted = {n.upper() for n in BUILD_EGRESS_NAMES}
    for key, value in env.items():
        if key.upper() not in wanted:
            continue
        for item in value.replace(",", " ").split():
            _name, named, rest = item.partition("=")
            url = rest if named and urlsplit(rest).netloc else item
            parts = urlsplit(url)
            if not parts.netloc:
                parts = urlsplit(f"//{url}")
            try:
                port = parts.port or _DEFAULT_PORTS.get(parts.scheme, 80)
            except ValueError:
                continue
            if parts.hostname and (parts.hostname, port) not in found:
                found.append((parts.hostname, port))
    return found


def build_carves(env: dict[str, str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(forwards, allow_hosts)`` for a build on a host with no platform
    of its own beside it (a compute machine on another host): a loopback
    endpoint's port spliced, a private address carved, never the cloud's
    metadata addresses. The platform's own host uses its stricter rule."""
    import ipaddress
    import socket
    forwards: list[str] = []
    allow: list[str] = []
    for host, port in build_endpoints(env):
        try:
            addrs = {i[4][0] for i in socket.getaddrinfo(host, None)}
        except OSError:
            continue
        for addr in sorted(str(a) for a in addrs):
            ip = ipaddress.ip_address(addr)
            if ip.is_loopback:
                if str(port) not in forwards:
                    forwards.append(str(port))
            elif (ip.is_private or ip.is_link_local) and addr not in _NEVER_CARVED and addr not in allow:
                allow.append(addr)
    return tuple(forwards), tuple(allow)


#: The cloud metadata endpoints: no carve ever reopens them.
_NEVER_CARVED = frozenset({"169.254.169.254", "169.254.170.2", "fd00:ec2::254"})


#: Ubuntu's AppArmor rule on unprivileged user namespaces: at "1" only a
#: process whose profile grants ``userns`` may create one.
_APPARMOR_USERNS_SYSCTL = "/proc/sys/kernel/apparmor_restrict_unprivileged_userns"
#: Debian's older switch on the same capability: "0" turns them off for the
#: whole kernel.
_DEBIAN_USERNS_SYSCTL = "/proc/sys/kernel/unprivileged_userns_clone"
#: pasta opens the sandbox's tap through this device. A container has it only
#: when its compose file maps it in.
_TUN_DEVICE = "/dev/net/tun"
_TUN_WORDS = re.compile(r"\btun\b|\btap\b", re.IGNORECASE)
#: A probe's own words for a user namespace it could not make. A kernel
#: switch is the cause only then: the compute container reads its host's
#: switch, which stays 1 on a host whose profile is installed.
_USERNS_WORDS = re.compile(r"namespace|uid map|unshare|clone", re.IGNORECASE)
#: Said with a failed probe whose cause this host cannot name.
_DOCTOR_SENTENCE = ("Run scripts/sandbox-doctor.sh where the sessions run to find what refuses it (in a Docker "
                    "install: docker compose exec otodock-compute /opt/otodock/scripts/sandbox-doctor.sh).")


def _reads(path: str, value: str) -> bool:
    """Whether a kernel switch reads ``value`` (False when it is not there)."""
    try:
        with open(path, encoding="ascii") as fh:
            return fh.read().strip() == value
    except (OSError, ValueError):
        return False


def tun_device_problem() -> str | None:
    """Why this host has no ``/dev/net/tun`` character device, or None."""
    try:
        if not stat.S_ISCHR(os.stat(_TUN_DEVICE).st_mode):
            return f"{_TUN_DEVICE} is not a character device"
    except OSError:
        return f"{_TUN_DEVICE} is missing"
    return None


def tun_sentence(why: str) -> str:
    """The refusal for a host whose launcher cannot open its tap."""
    return (f"The sandbox's network launcher cannot open its tap device ({why}). In a Docker install the "
            "compute service needs the device (docker-compose.yml: devices: /dev/net/tun). On a server "
            "without Docker load the tun module (sudo modprobe tun). Then restart.")


def probe_cause(detail: str) -> str:
    """What a failed probe most likely means on this host, with the fix an
    admin can make, or "": the tap device when the probe's own words name
    it, else, when they name a user namespace, the kernel switch that keeps
    a process from making one (AppArmor's rule, Debian's switch)."""
    if _TUN_WORDS.search(detail):
        return tun_sentence(detail)
    if not _USERNS_WORDS.search(detail):
        return ""
    if _reads(_APPARMOR_USERNS_SYSCTL, "1"):
        return ("This host lets only a process with an AppArmor profile make a user namespace "
                "(kernel.apparmor_restrict_unprivileged_userns=1, the default since Ubuntu 24.04). In a "
                "Docker install run `sudo scripts/setup-apparmor-userns.sh` on the Docker host, then "
                "restart with scripts/compose.sh. Running `docker compose` yourself, set "
                "OTODOCK_APPARMOR_PROFILE=otodock_userns in .env. On a server without Docker the distribution's AppArmor "
                "profiles for bwrap and passt must be installed. Do not set the switch to 0: that turns "
                "the protection off for the whole host.")
    if _reads(_DEBIAN_USERNS_SYSCTL, "0"):
        return ("Unprivileged user namespaces are off on this host (kernel.unprivileged_userns_clone=0). "
                "Turn them on with `sysctl kernel.unprivileged_userns_clone=1` and keep it in a file "
                "under /etc/sysctl.d.")
    return ""


def preflight(host: RenderHost, *, timeout_s: float = 20.0) -> str | None:
    """The shape every session on this host runs, once: the launcher
    wrapping one bwrap level with the sessions' namespaces and a fresh
    ``/proc``, running ``true``. ``None`` when it ran; else what went wrong,
    for the host to refuse to serve sessions with. A failure whose cause
    this host can name (``probe_cause``) says the cause and its fix, and
    any other failed probe says where the doctor script is run."""
    for tool in ("pasta", "bwrap", "ip"):
        if not shutil.which(tool):
            return f"{tool} is not installed"
    if not os.path.isfile(host.launcher):
        return f"the launcher is missing at {host.launcher}"
    no_tun = tun_device_problem()
    if no_tun is not None:
        return tun_sentence(no_tun)
    desc = SandboxDescription(
        agent="", binds=(), mcp_binds=(), mcps_root="", extra_binds=(), cwd="/",
        proxy_port=1, forwards=("1",), beneath=False,
    )
    probe_host = RenderHost(
        agents_root=host.agents_root, mcps_root=host.mcps_root, empty_dir=host.empty_dir,
        launcher=host.launcher, uid=host.uid, gid=host.gid, resolv_conf=host.resolv_conf,
    )
    argv = render(desc, probe_host, ["true"])
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return f"the sandbox probe did not answer within {timeout_s:.0f} s"
    except OSError as e:
        return f"the sandbox probe could not start: {e}"
    if out.returncode != 0:
        said = (out.stderr or out.stdout or "").strip()
        last = said.splitlines()[-1] if said else "no output"
        cause = probe_cause(said)
        return f"the sandbox probe failed (exit {out.returncode}): {last}. {cause or _DOCTOR_SENTENCE}"
    return None


def pasta_build_date(version_line: str) -> str:
    """`YYYY_MM_DD` of a passt version line: an upstream tag
    (`2026_10_02.cba3570`), a git build (`2026_06_11.a9c61ff-31-g…`) or a
    Debian version (`0.0~git20230309.7c7625d-1`); "" when it names none."""
    m = (re.search(r"(\d{4})_(\d{2})_(\d{2})", version_line)
         or re.search(r"git(\d{4})(\d{2})(\d{2})", version_line))
    return "_".join(m.groups()) if m else ""


def _first_line(argv: list[str]) -> str:
    """The first line a quick command prints, "" when it cannot run."""
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (out or "").strip().splitlines()
    return lines[0] if lines else ""


def pasta_version() -> tuple[str, str]:
    """(the version to name, its build date) of the pasta on PATH.

    Debian's package prints no date (built outside git: bookworm's says
    "pasta unknown version", trixie's prints nothing), so a binary under
    /usr/bin with no date in its line falls back to the package's own
    version."""
    exe = shutil.which("pasta")
    if not exe:
        return "", ""
    line = _first_line([exe, "--version"])
    date = pasta_build_date(line)
    if not date and os.path.realpath(exe).startswith("/usr/bin/"):
        package = _first_line(["dpkg-query", "-W", "-f=${Version}", "passt"])
        if pasta_build_date(package):
            return f"pasta (package passt {package})", pasta_build_date(package)
    return line, date


def pasta_floor_note(floor: str) -> str:
    """What to say, never to refuse with, when this host's pasta is older
    than ``floor`` (``PASST_MIN_VERSION``, ``YYYY_MM_DD``): the floor names
    the build the launcher's options are written against, and an older
    pasta still runs it. "" when it is not older or cannot be compared."""
    line, have = pasta_version()
    if not floor or not have or have >= floor:
        return ""
    return (f"{line} is older than {floor}, the pasta the sandbox's launcher options are written against. "
            "Sessions still run. In a Docker install take this release's image, which carries the pinned "
            "build. On a server without Docker run scripts/install-baseline-tools.sh again.")


def self_hash() -> str:
    """SHA256 of this module's source, for the satellite's drift check."""
    try:
        with open(__file__, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


__all__ = [
    "Bind", "DNS_FORWARD_ADDR", "PLATFORM_MARKER", "PROXY_FORWARD", "ROOT_ABS", "ROOT_AGENT", "ROOT_EMPTY", "ROOT_NULL",
    "ROOT_MCPS", "SSH_AGENT_SOCKET", "EXIT_BUNDLE_PATH", "RenderHost", "RenderRefused", "SandboxDescription", "agent_root",
    "check_folders",
    "BUILD_EGRESS_NAMES", "BUILD_MASKED", "BUILD_OOM_SCORE_ADJ", "BUILD_OUTPUTS", "BUILD_SETTING_NAMES",
    "BUILD_TRUST_DIR_FILE", "BUILD_TRUST_DIR_NAME", "BUILD_TRUST_NAMES", "KIND_BUILD",
    "KIND_SESSION", "build_carves", "build_data_dirs", "build_description", "build_endpoints",
    "pasta_build_date", "pasta_floor_note", "pasta_version", "probe_cause", "tun_device_problem", "tun_sentence",
    "build_ro_paths", "oom_raise", "prepare_build_step",
    "cli_install_ro_binds", "effective_tmp_cap_mb", "host_resolv_has_loopback_ns",
    "launcher_prefix", "mem_total_mb", "preflight", "probe_bwrap_size", "render",
    "self_hash", "source_of", "ssh_agent_source", "system_mounts", "exit_bundle_source",
]
