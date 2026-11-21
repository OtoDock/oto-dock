"""This machine as the host of the platform's sandbox (compute mode).

A session on a compute machine is the platform's own: the proxy readied
its files in the shared agents folder and planned its spawn (the
description of its sandbox, the argv with the engine's binary by its bare
name, the environment). This module renders that description with this
machine's values (``_vendored/sandbox_render.py``, the proxy's module
byte for byte): its agents and tools folders, its empty mask folder, the
vendored launcher, its uid and gid, the local link's port the proxy port
is spliced onto, its resolver file when its own resolver is a loopback
stub, the ``/tmp`` cap its bubblewrap takes, and its own pinned binary
with the read-only binds that make it runnable. Nothing of the machine is
added to the environment, nothing is written into the agents folder, no
path is translated: the sandbox is the proxy's.

A tool's build runs in the same sandbox (``build_command``). Where its
requests may go is this machine's own to say on another server (the
render's ``build_carves``). The install's own machine, which keeps the
community tools beside the platform, takes it from the platform's sync
frame instead (``note_egress``): its builds open what the platform's own
rule opened and nothing else.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import ipaddress
import logging
import os
import re
import shutil
import tempfile
from functools import cache
from pathlib import Path

from .._vendored import sandbox_render as render
from ..config import SatelliteConfig, otodock_dir
from . import credential_exit, ssh_agent

logger = logging.getLogger("satellite")

#: The OOM score of every process a compute session runs: above this
#: machine's own satellite (the compute service's 250) and the proxy (0),
#: raised only, in the session's first process, so nothing it forks starts
#: lower.
SESSION_OOM_SCORE_ADJ = 500
#: The shell snippet that raises it (the render's, shared with a build's step).
_RAISE_OOM = render.oom_raise([], SESSION_OOM_SCORE_ADJ)[2]
#: The CPU priority of every process a session runs: below this machine's
#: own satellite, so a wave of starts never takes the core its link needs.
SESSION_NICE = 10

#: The state this machine keeps for the sandbox: the empty folder every
#: mask binds from and the generated resolver file. Under the machine's own
#: OtoDock folder, never under the agents folder.
STATE_DIRNAME = "compute"
_RESOLV_NAME = "netns-resolv.conf"


def is_compute(payload: dict) -> bool:
    """Whether a start payload is a compute session's: it carries the
    sandbox description the platform rendered nothing of."""
    return isinstance(payload.get("sandbox"), dict)


def spawn_env(payload: dict) -> dict[str, str]:
    """The spawn's environment: the plan's exactly. The platform built it
    for its sandbox, so this machine adds nothing and reads nothing of its
    own into it."""
    return {str(k): str(v) for k, v in (payload.get("env") or {}).items()}


def state_dir() -> Path:
    return otodock_dir() / STATE_DIRNAME


def launcher_path() -> Path:
    return Path(__file__).resolve().parents[1] / "_vendored" / "oto-sandbox-net"


@cache
def _bwrap_has_size() -> bool:
    return render.probe_bwrap_size()


def _resolv_conf() -> str:
    """The generated resolver file when this host's resolver is a loopback
    stub a sandbox cannot reach (the launcher forwards the address it
    names); ``""`` when the host's own file serves."""
    if not render.host_resolv_has_loopback_ns():
        return ""
    path = state_dir() / _RESOLV_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    text = f"nameserver {render.DNS_FORWARD_ADDR}\n"
    if not path.is_file() or path.read_text() != text:
        path.write_text(text)
    return str(path)


def render_host(sat_config: SatelliteConfig, *, tunnel_port: int, tmp_size_mb: int = 0,
                bin_path: str = "") -> render.RenderHost:
    """This machine's values for the render. ``tunnel_port`` is the local
    link the proxy port is spliced onto; ``tmp_size_mb`` the description's
    cap, bounded here by this host's RAM; ``bin_path`` the engine's resolved
    binary, whose install folder is bound read-only. The SSH agent sockets
    this machine serves live in ``ssh_agent.socket_dir()``: a description
    that names one binds it from there. The credential exit this machine
    serves gives the exit port's splice target and the bundle a description
    with ``exit_port`` binds (``credential_exit.render_values``: both empty
    while the exit is not up, so such a description is refused)."""
    empty = state_dir() / ".empty"
    empty.mkdir(parents=True, exist_ok=True)
    exit_port, exit_bundle = credential_exit.render_values()
    return render.RenderHost(
        agents_root=str(sat_config.agents_dir),
        mcps_root=str(sat_config.mcps_dir),
        empty_dir=str(empty),
        launcher=str(launcher_path()),
        uid=os.getuid(),
        gid=os.getgid(),
        proxy_target_port=int(tunnel_port or 0),
        resolv_conf=_resolv_conf(),
        tmp_cap_mb=render.effective_tmp_cap_mb(tmp_size_mb, _bwrap_has_size(), render.mem_total_mb()),
        extra_ro_binds=tuple(render.cli_install_ro_binds(bin_path)) if bin_path else (),
        exit_target_port=exit_port,
        exit_bundle=exit_bundle,
        ssh_agent_dir=str(ssh_agent.socket_dir()),
    )


def with_oom_raise(cmd: list[str]) -> list[str]:
    """``cmd`` run behind the raise of its own OOM score: the shell ``exec``s
    it, so the pid and the argv are the command's own."""
    return ["/bin/sh", "-c", _RAISE_OOM, "oto-session", *cmd]


@cache
def _nice_bin() -> str:
    nice = shutil.which("nice") or ""
    if not nice:
        logger.warning("nice not found: session processes start at the satellite's own priority")
    return nice


def niced(cmd: list[str]) -> list[str]:
    """``cmd`` run at ``SESSION_NICE``, where the machine has ``nice`` (a
    Windows computer has none). ``nice`` ``exec``s it, so the pid and the
    argv are the command's own, and everything it starts inherits the
    priority."""
    from .. import config
    nice = _nice_bin() if config.HOST.posix else ""
    return [nice, "-n", str(SESSION_NICE), *cmd] if nice else list(cmd)


def paired_command(cmd: list[str]) -> list[str]:
    """A paired computer's session spawn, below the satellite's priority: on
    Linux behind the same raise, so the kernel's last resort ends a session's
    process before the satellite (which keeps its default score); elsewhere
    ``cmd`` as it is."""
    from .. import config
    return niced(with_oom_raise(cmd) if config.HOST.oom_score else list(cmd))


async def spawn_command(payload: dict, sat_config: SatelliteConfig, binary: str, *,
                        for_pty: bool = False, inner: list[str] | None = None) -> list[str]:
    """The command a compute session runs: the description rendered with
    this machine's values around the plan's argv, the engine's binary
    resolved to this machine's pinned install (the plan names it bare).
    ``inner`` replaces the plan's argv (the Codex app-server client adds
    its own)."""
    from ..host.cli_versions import resolve_spawn_bin_async
    desc = render.SandboxDescription.from_dict(payload["sandbox"])
    bin_path = await resolve_spawn_bin_async(binary, sat_config.bin_hint(binary), for_pty=for_pty)
    argv = list(payload.get("argv") or [binary]) if inner is None else list(inner)
    return niced(render_command(desc, sat_config, tunnel_port=link_port(payload), bin_path=bin_path,
                                argv=argv, swap_binary=inner is None))


def render_command(desc: render.SandboxDescription, sat_config: SatelliteConfig, *,
                   tunnel_port: int, argv: list[str], bin_path: str = "",
                   swap_binary: bool = False) -> list[str]:
    """The one render of a sandboxed command on this machine: the
    description, this host's values with the description's ``/tmp`` cap, and
    ``argv``, whose first word becomes ``bin_path`` when ``swap_binary`` is
    set. A session, an app server and a script run all come through here."""
    argv = list(argv)
    if swap_binary and argv:
        argv[0] = bin_path
    refuse_other_tools_path(desc, sat_config)
    host = render_host(sat_config, tunnel_port=tunnel_port,
                       tmp_size_mb=int(desc.tmp_size_mb or 0), bin_path=bin_path)
    return with_oom_raise(render.render(desc, host, argv))


async def ready_own_copy(payload: dict, sat_config: SatelliteConfig, session_id: str,
                         agent_dir: Path) -> bool:
    """On a compute machine on another host (``own_copy``): the files the
    platform prepared for the session, written under this machine's own
    agent folder before the render (``session_files.materialize_agent_files``).
    True when this machine keeps its own copy: the session's changes are its
    to report, and the files go at its close."""
    if not getattr(sat_config, "own_copy", False):
        return False
    import asyncio

    from . import session_files, session_homes
    spec = payload.get("session_home")
    rel = payload.get("claude_dir_relative") or payload.get("codex_dir_relative")
    try:
        await asyncio.to_thread(session_files.materialize_agent_files, payload, session_id, agent_dir)
        # The session's home: its files came with the others, its links are
        # this copy's, in the engine's own folder.
        if isinstance(spec, dict) and rel:
            await asyncio.to_thread(session_homes.open_home, agent_dir / rel, session_id, spec)
            # The platform clears its own disk's Codex store, never this copy
            # (``spawn._make_home``): no session of this machine runs from the
            # store once its satellite runs this version.
            if payload.get("codex_dir_relative") and not payload.get("claude_dir_relative"):
                from .codex_files import drop_store_leftovers
                await asyncio.to_thread(drop_store_leftovers, agent_dir, rel)
    except BaseException:
        # The session will not run: its close does not know this copy yet,
        # so what was written by its id (the sign-in among it) goes here.
        session_files.wipe_agent_files(session_id)
        if rel:
            session_homes.close_home(agent_dir / rel, session_id, spec)
        raise
    return True


def refuse_other_tools_path(desc: render.SandboxDescription, sat_config: SatelliteConfig) -> None:
    """A compute machine on another host keeps its tools folder at the
    platform's own tools path: the session's tool config names each tool by
    that path and a tool's environment carries the path it was built at (the
    platform's image puts both at ``/opt/otodock/mcps``). Another path is
    refused with the reason, before anything runs."""
    if not getattr(sat_config, "own_copy", False) or not desc.mcps_root:
        return
    if Path(desc.mcps_root) != Path(sat_config.mcps_dir):
        raise render.RenderRefused(
            f"this machine's tools folder is {sat_config.mcps_dir}, and it must be the platform's "
            f"tools path, {desc.mcps_root}")


def interpreter_roots(sat_config: SatelliteConfig, mcp_dir: Path) -> list[Path]:
    """Where a tool's Python must live for this machine's sandbox to run it.
    A community tool: its own folder and the community interpreter folder
    alone, so its venv is made on a uv-managed interpreter that travels with
    the community tools and no new image of this machine removes. Any other
    tool: the system folders the render binds too. The installer makes a
    venv whose interpreter lies elsewhere again on a managed one."""
    from .._vendored import mcp_installer
    own = mcp_installer.uv_python_dir_for(Path(mcp_dir)).resolve()
    if mcp_installer.is_community(Path(mcp_dir)):
        return [own, Path(mcp_dir).resolve()]
    roots = [Path(p).resolve() for p in (*render._SYSTEM_RO_BINDS, *render._SYSTEM_RO_OPTIONAL)
             if not p.startswith("/snap")]
    roots.extend(d.resolve() for d in mcp_installer.interpreter_dirs(Path(sat_config.mcps_dir)))
    roots.append(Path(mcp_dir).resolve())
    return roots


def build_host(sat_config: SatelliteConfig) -> render.RenderHost:
    """This machine as the host of one step of a tool's build: its folders,
    its empty mask folder, its launcher, uid and gid and resolver swap. It
    renders its own absolute sources because it made the description itself
    (a build's description never travels)."""
    empty = state_dir() / ".empty"
    empty.mkdir(parents=True, exist_ok=True)
    return render.RenderHost(
        agents_root=str(sat_config.agents_dir), mcps_root=str(Path(sat_config.mcps_dir).resolve()),
        empty_dir=str(empty), launcher=str(launcher_path()), uid=os.getuid(), gid=os.getgid(),
        resolv_conf=_resolv_conf(), allow_abs=True,
    )


def build_command(sat_config: SatelliteConfig, cmd: list[str], env: dict[str, str], step: str,
                  mcp_dir: Path, step_dir: Path) -> tuple[list[str], dict[str, str]]:
    """The argv that runs ``cmd`` as ``step`` of a tool's build in this
    machine's sandbox, and its environment: the render's build table (the
    tool's folder read-only, the step's outputs and scratch writable), this
    machine's own proxy and private indexes carved. The install's own
    machine (``own_tools``) carves what the platform's sync named instead
    (``platform_egress``), and nothing when it named nothing: the render's
    own rule opens any private address, the platform's among them. Blocking
    (folder writes)."""
    from .._vendored import mcp_installer
    tool_dir = Path(mcp_dir).resolve()
    carves = None
    if getattr(sat_config, "own_tools", False):
        env, *carves = platform_egress(env)
    step_env = render.prepare_build_step(str(tool_dir), step, str(step_dir), env)
    own = mcp_installer.uv_python_dir_for(tool_dir)
    own.mkdir(exist_ok=True)
    forwards, allow = carves if carves is not None else render.build_carves(step_env)
    desc = render.build_description(
        str(tool_dir), step, mcps_root=str(Path(sat_config.mcps_dir).resolve()), scratch=str(step_dir),
        interpreter_dirs=tuple(str(d) for d in mcp_installer.interpreter_dirs(Path(sat_config.mcps_dir).resolve())),
        own_interpreter_dir=str(own), data_dirs=render.build_data_dirs(str(tool_dir)),
        ro_paths=render.build_ro_paths(cmd, step, step_env), forwards=forwards, allow_hosts=allow,
        hosted=hosted_install(),
    )
    return with_oom_raise(render.render(desc, build_host(sat_config), list(cmd))), step_env


def note_hosted(hosted: bool) -> None:
    """What the platform's last sync said (``hosted`` in its frame): a hosted
    install's builds block its ranges too (the render's ``--hosted``), the
    rebuilds at this machine's next start as well, so it is kept here."""
    mark = state_dir() / "hosted"
    if hosted and not mark.exists():
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text("1\n")
    elif not hosted and mark.exists():
        mark.unlink()


def hosted_install() -> bool:
    return (state_dir() / "hosted").exists()


#: The platform's egress settings for the builds of the sync frame this
#: machine is handling, in memory alone: an index or proxy address may
#: carry a user and a password, and the platform owns it. This machine uses
#: the values for that frame's builds and writes them nowhere. The frame's
#: trust files (certificates, no secret) lie in its state folder while the
#: frame runs, since a build reads them by path.
_egress: dict | None = None
_EGRESS_TRUST_DIR = "egress-trust"
#: The most a sync frame's ``egress`` block may carry. Anything past a
#: bound, or outside the names the render reads, is dropped.
_EGRESS_VALUE_MAX = 4096
_EGRESS_TRUST_MAX, _EGRESS_TRUST_BYTES = 8, 1 << 20
_EGRESS_FORWARDS_MAX, _EGRESS_HOSTS_MAX = 16, 32
_EGRESS_NAMES = frozenset(n.upper() for n in (*render.BUILD_EGRESS_NAMES, *render.BUILD_SETTING_NAMES))
#: The trust names that name one file. The one that names a folder travels
#: as that folder's certificates (``cert_dir``).
_EGRESS_TRUST_NAMES = frozenset(n.upper() for n in render.BUILD_TRUST_NAMES) - {render.BUILD_TRUST_DIR_NAME}
_TRUST_FILE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: The certificate folder as this machine keeps it, inside the trust
#: folder: at most this many files, their bytes bounded as one file's.
_EGRESS_CERT_DIR, _EGRESS_CERT_FILES_MAX = "cert-dir", 512
_CERT_FILE_RE = re.compile(render.BUILD_TRUST_DIR_FILE)


def _egress_env(raw, dropped: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in (raw.items() if isinstance(raw, dict) else ()):
        if (isinstance(name, str) and name.upper() in _EGRESS_NAMES and isinstance(value, str)
                and 0 < len(value) <= _EGRESS_VALUE_MAX and "\x00" not in value):
            out[name] = value
        else:
            dropped.append(f"env {str(name)[:40]}")
    return out


def _egress_forwards(raw, dropped: list[str]) -> list[str]:
    out: list[str] = []
    for port in (raw if isinstance(raw, list) else ()):
        text = port if isinstance(port, str) else ""
        if text.isascii() and text.isdigit() and len(text) <= 5 and 1 <= int(text) <= 65535 \
                and len(out) < _EGRESS_FORWARDS_MAX:
            if text not in out:
                out.append(text)
        else:
            dropped.append(f"forward {str(port)[:20]}")
    return out


def _egress_hosts(raw, dropped: list[str]) -> list[str]:
    out: list[str] = []
    for host in (raw if isinstance(raw, list) else ()):
        try:
            ip = ipaddress.ip_address(host) if isinstance(host, str) else None
        except ValueError:
            ip = None
        # An IPv4 address written as an IPv6 one is judged as the IPv4 one.
        probe = getattr(ip, "ipv4_mapped", None) or ip
        if (probe is None or probe.is_loopback or probe.is_unspecified
                or {str(ip), str(probe)} & render._NEVER_CARVED or len(out) >= _EGRESS_HOSTS_MAX):
            dropped.append(f"host {str(host)[:45]}")
        elif str(ip) not in out:
            out.append(str(ip))
    return out


def _egress_trust(raw, dropped: list[str]) -> tuple[dict[str, str], dict[str, bytes]]:
    """``({ENV NAME: file name}, {file name: bytes})`` of the block's trust
    files: one file per name, kept under the name's own prefix so two names
    never share a file."""
    names: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for entry in (raw if isinstance(raw, list) else ()):
        env = entry.get("env") if isinstance(entry, dict) else None
        name = entry.get("name") if isinstance(entry, dict) else None
        data = entry.get("data_b64") if isinstance(entry, dict) else None
        label = f"trust {str(env)[:30]}"
        if not (isinstance(env, str) and env.upper() in _EGRESS_TRUST_NAMES and env.upper() not in names
                and isinstance(name, str) and _TRUST_FILE_RE.fullmatch(name)
                and isinstance(data, str) and len(data) <= _EGRESS_TRUST_BYTES * 4 // 3 + 4
                and len(names) < _EGRESS_TRUST_MAX):
            dropped.append(label)
            continue
        try:
            decoded = base64.b64decode(data, validate=True)
        except (binascii.Error, ValueError):
            dropped.append(label)
            continue
        if not decoded or len(decoded) > _EGRESS_TRUST_BYTES:
            dropped.append(label)
            continue
        kept = f"{env.upper()}-{name}"
        names[env.upper()] = kept
        files[kept] = decoded
    return names, files


def _egress_cert_dir(raw, dropped: list[str]) -> dict[str, bytes]:
    """``{file name: bytes}`` of the block's certificate folder, under the
    names a TLS library looks up. All of it or none: one entry outside the
    bounds drops the folder, since a part of a trust store would read as
    the whole of it."""
    files: dict[str, bytes] = {}
    total = 0
    for entry in (raw if isinstance(raw, list) else ()):
        name = entry.get("name") if isinstance(entry, dict) else None
        data = entry.get("data_b64") if isinstance(entry, dict) else None
        decoded = b""
        if (isinstance(name, str) and _CERT_FILE_RE.fullmatch(name) and name not in files
                and isinstance(data, str) and len(data) <= _EGRESS_TRUST_BYTES * 4 // 3 + 4
                and len(files) < _EGRESS_CERT_FILES_MAX):
            with contextlib.suppress(binascii.Error, ValueError):
                decoded = base64.b64decode(data, validate=True)
        total += len(decoded)
        if not decoded or total > _EGRESS_TRUST_BYTES:
            dropped.append("cert_dir")
            return {}
        files[name] = decoded
    return files


def _write_private(path: Path, data: bytes) -> None:
    """``data`` in ``path``, readable by this machine's account alone."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(data)


def note_egress(block) -> None:
    """What the platform's sync frame says of where its builds' requests go
    (``egress``): the proxy, index and setting names of the platform's
    environment, its trust files and certificate folder, and what its own
    rule carved for them. Held in memory for the builds of that frame alone
    (``clear_egress`` at its end): the values are never written to a file,
    a log or an argument, and reach a build as its own environment. A
    frame with none holds none. Everything outside the block's bounds is
    dropped and named once in the log, by its name alone. Blocking."""
    global _egress
    folder = state_dir() / _EGRESS_TRUST_DIR
    dropped: list[str] = []
    block = block if isinstance(block, dict) else {}
    env = _egress_env(block.get("env"), dropped)
    trust, files = _egress_trust(block.get("trust"), dropped)
    certs = _egress_cert_dir(block.get("cert_dir"), dropped)
    forwards = _egress_forwards(block.get("forwards"), dropped)
    hosts = _egress_hosts(block.get("allow_hosts"), dropped)
    if dropped:
        logger.warning("builds: the platform's egress block named what this machine does not take: %s",
                       ", ".join(dropped[:12]))
    clear_egress()
    if not (env or trust or certs or forwards or hosts):
        return
    if files or certs:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name, data in files.items():
            _write_private(folder / name, data)
    if certs:
        (folder / _EGRESS_CERT_DIR).mkdir(mode=0o700)
        for name, data in certs.items():
            _write_private(folder / _EGRESS_CERT_DIR / name, data)
    _egress = {"env": env, "trust": trust, "cert_dir": bool(certs), "forwards": tuple(forwards),
               "allow_hosts": tuple(hosts)}


def clear_egress() -> None:
    """The frame's builds ended (or a new frame begins): its egress values
    are forgotten and its trust files removed. Blocking."""
    global _egress
    _egress = None
    shutil.rmtree(state_dir() / _EGRESS_TRUST_DIR, ignore_errors=True)


def platform_egress(env: dict[str, str]) -> tuple[dict[str, str], tuple[str, ...], tuple[str, ...]]:
    """``(env, forwards, allow_hosts)`` of a build on the install's own
    machine: ``env`` with the platform's proxy, index, setting and trust
    names over this machine's own (a trust name points at the file or the
    folder kept here for the frame), and the carves the platform named.
    Outside a frame, or in one that named none: ``env`` as it is and no
    carve."""
    held = _egress
    if held is None:
        return dict(env), (), ()
    named = dict(held["env"])
    folder = state_dir() / _EGRESS_TRUST_DIR
    for name, file_name in held["trust"].items():
        if (folder / file_name).is_file():
            named[name] = str(folder / file_name)
    if held["cert_dir"] and (folder / _EGRESS_CERT_DIR).is_dir():
        named[render.BUILD_TRUST_DIR_NAME] = str(folder / _EGRESS_CERT_DIR)
    taken = {name.upper() for name in named}
    out = {k: v for k, v in env.items() if k.upper() not in taken}
    out.update(named)
    return out, held["forwards"], held["allow_hosts"]


def sweep_build_scratch() -> int:
    """Remove the scratch a killed build left under this machine's state
    folder; run at the satellite's start, before any build. Answers how many
    folders went."""
    from .._vendored import mcp_installer
    return mcp_installer.sweep_folders(state_dir() / "builds")


def build_run(sat_config: SatelliteConfig):
    """The ``run`` of every build step on this compute machine
    (``mcp_installer.install_mcp(run=)``): the step in this machine's sandbox,
    a fresh scratch folder of its own, removed when the step ends."""
    from .._vendored import mcp_installer
    scratch_root = state_dir() / "builds"

    async def run(cmd: list[str], *, cwd: str | None, env: dict[str, str], timeout: float,
                  step: str, mcp_dir: Path) -> tuple[int, str]:
        scratch_root.mkdir(parents=True, exist_ok=True)
        step_dir = Path(tempfile.mkdtemp(prefix=f"{step}-", dir=scratch_root))
        try:
            argv, step_env = await asyncio.to_thread(build_command, sat_config, cmd, env, step,
                                                     Path(mcp_dir), step_dir)
            return await mcp_installer._run_bounded(argv, cwd=str(step_dir), env=step_env, timeout=timeout)
        finally:
            try:
                await asyncio.to_thread(mcp_installer.remove_tree, step_dir)
            except OSError as exc:  # the start's sweep takes it
                logger.warning("could not remove the build scratch %s: %s", step_dir, exc)
    return run


def link_port(payload: dict) -> int:
    """The local link port the proxy port is spliced onto, named by the
    payload. Without one the render would splice the bare proxy port onto
    this host's loopback, the platform's own listener on bare metal:
    refused."""
    port = int(payload.get("tunnel_port") or 0)
    if port <= 0:
        raise render.RenderRefused("the compute session names no link port to splice the proxy port onto")
    return port


async def sandbox_prefix(payload: dict, sat_config: SatelliteConfig, binary: str) -> tuple[list[str], str]:
    """``(prefix, bin_path)``: the rendered launcher and bubblewrap up to
    the ``--`` that precedes the command, and the engine's resolved binary,
    for a client that assembles the command itself."""
    from ..host.cli_versions import resolve_spawn_bin_async
    desc = render.SandboxDescription.from_dict(payload["sandbox"])
    refuse_other_tools_path(desc, sat_config)
    bin_path = await resolve_spawn_bin_async(binary, sat_config.bin_hint(binary))
    host = render_host(sat_config, tunnel_port=link_port(payload),
                       tmp_size_mb=int(desc.tmp_size_mb or 0), bin_path=bin_path)
    return niced(with_oom_raise(render.render(desc, host, []))), bin_path


def preflight(sat_config: SatelliteConfig) -> str | None:
    """Whether this machine can run the platform's sandbox at all, once at
    start: ``None`` when the probe ran, else the reason to refuse to sign
    in as a compute machine."""
    if not sat_config.agents_dir.is_dir():
        return f"the agents folder is not there: {sat_config.agents_dir}"
    if not sat_config.mcps_dir.is_dir():
        return f"the tools folder is not there: {sat_config.mcps_dir}"
    return render.preflight(render_host(sat_config, tunnel_port=0))
