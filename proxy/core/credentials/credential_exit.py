"""The platform's half of the credential exit. Every session runs on a
machine, and that machine's satellite runs the relay (``exit_relay.Relay``,
vendored byte for byte) on its loopback; the platform gives each session its
door and its values (CREDENTIALS.md "The credential exit"):

* **The door** (``machine_block``): the session's secret, derived per
  session (``_door_secret``, stored nowhere), the hash its pushed values are
  keyed by, its entries and its carves, nothing of a value.
* **The variables** a session's processes get (``attach_sandbox`` on a
  compute machine, ``machine_env`` on a paired computer): the machine's exit
  as their HTTPS proxy with the session's secret, the loopback names, the
  carves and the engines' hosts direct, the trust variables at the machine's
  bundle.
* **The value** behind a placeholder (``exit_resolution``): the command-line
  credential's token, or the gateway's resolution (the bearer allowlist under
  the manifest's name, the egress guard, the value or the token file read
  fresh). The push registry resolves every value through it.
* **The judge** of a narrowed reach (``judge_request``), asked by a machine's
  exit through the command-line credential's route.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import config
from core.credentials import exit_relay, mcp_broker, mcp_gateway
from core.credentials.exit_relay import (
    ENGINE_HOSTS,
    ENV_NAMES,
    MAX_BODY_BYTES,
    PLACEHOLDER_PREFIX,
    REFUSAL_HOST,
    REFUSAL_PLACE,
    REFUSAL_PLAIN,
    REFUSAL_WHERE,
    Judged,
    new_placeholder,
)

logger = logging.getLogger("otodock.credential-exit")

#: The port a compute session dials its exit on inside its namespace, the
#: first try: the render splices it onto the machine's listener
#: (``RenderHost.exit_target_port``), and ``_sandbox_exit_port`` moves past
#: one the description already forwards.
_SANDBOX_EXIT_PORT = 41443


#: The engines' hosts with a leading dot, beside the bare names the relay's
#: composer adds: a client that reads a bare name as that one host (Node's
#: own proxy support, undici) keeps the engines' subdomains direct by these.
_ENGINE_SUBDOMAINS = tuple(f".{host}" for host in ENGINE_HOSTS)


def _relay_env(*, port: int, credential: str, bundle_path: str, allow_hosts: Iterable[str] = (),
               direct_hosts: Iterable[str] = (), upstream: bool = False) -> dict[str, str]:
    """``exit_relay.session_env`` with the engines' subdomains direct too:
    the one composer of every session's variables on this side. Behind the
    machine's own outgoing proxy (``upstream``) the engines' hosts are not
    kept direct: a network whose only way out is that proxy would cut them,
    so their traffic passes the exit as a plain tunnel through it."""
    return exit_relay.session_env(port=port, credential=credential, bundle_path=bundle_path,
                                  allow_hosts=allow_hosts,
                                  direct_hosts=(*direct_hosts, *(() if upstream else _ENGINE_SUBDOMAINS)),
                                  engines_direct=not upstream)


def own_direct(session_id: str, creds: Iterable[mcp_gateway.ExitCredential],
               env: Mapping[str, str]) -> dict[str, str]:
    """A tool's own proxy variables as it starts: where a host one of its
    exit entries declares sits under an engine's domain (``api.openai.com``
    under ``openai.com``), its ``HTTPS_PROXY`` names the tool's own proxy
    user (``exit_relay.TOOL_PROXY_USER``, the same secret), the one
    connection to such a host the exit ends; and where the session keeps a
    name that covers a declared host direct, its ``NO_PROXY`` drops that
    name, so the tool's requests toward the host pass the exit while the
    engine and every other program of the chat go as they go. The variables are the
    session's own (``env_of``), else the ones ``env`` names (a paired
    computer's bundle, ``machine_env``), else the composer's: after a
    restart a compute session's carves are not held here, and the exit
    dials them by the sandbox's rule, so a request toward one still arrives.
    Empty when neither holds. Computed at each
    start from the bundle's exit half and never stored in its environment,
    whose names a re-adoption resolves again."""
    hosts = {host.lower().removeprefix("*.") for cred in creds for host in cred.hosts}
    if not hosts:
        return {}
    held = env_of(session_id)
    out: dict[str, str] = {}
    proxy = held.get("HTTPS_PROXY") or env.get("HTTPS_PROXY") or ""
    user = f"//{exit_relay.PROXY_USER}:"
    if any(exit_relay.under_engine(host) for host in hosts) and proxy.startswith("http:" + user):
        tool = proxy.replace(user, f"//{exit_relay.TOOL_PROXY_USER}:", 1)
        out.update({"HTTPS_PROXY": tool, "https_proxy": tool})
    listed = held.get("NO_PROXY") or env.get("NO_PROXY")
    if listed is None:
        listed = _relay_env(port=0, credential="", bundle_path="")["NO_PROXY"]
    names = [name for name in listed.split(",") if name.strip()]
    kept = [name for name in names if not any(_covers(name, host) for host in hosts)]
    if len(kept) != len(names):
        value = ",".join(kept)
        out.update({"NO_PROXY": value, "no_proxy": value})
    return out


def _covers(name: str, host: str) -> bool:
    """Whether a ``NO_PROXY`` name sends ``host`` around the proxy, read the
    widest way a client reads one: the name, a leading dot or ``*.``, and
    every subdomain."""
    bare = name.strip().lower().lstrip("*").lstrip(".")
    return bool(bare) and (host == bare or host.endswith("." + bare))


# ---------------------------------------------------------------------------
# The sessions the exit serves
# ---------------------------------------------------------------------------


@dataclass
class _Session:
    carves: frozenset[str] = frozenset()
    # The variables ``attach_sandbox`` or ``machine_env`` gave the session
    # (``env_of``).
    env: dict[str, str] = field(default_factory=dict)
    # The secret its ``HTTPS_PROXY`` carries, which the machine's exit
    # admits (``machine_block``).
    secret: str = ""
    compute: bool = False


_sessions: dict[str, _Session] = {}


async def attach_sandbox(session_id: str, builder, extra_env: dict[str, str],
                         host_config_dir: str | Path, *, sandbox_config_dir: str,
                         direct_hosts: Iterable[str] = (), machine=None) -> None:
    """A compute session's half of the exit at its start: its carves
    registered, the exit's port on the sandbox description
    (``SandboxBuilder.exit_port``, spliced by the render onto the machine's
    listener) and the variables in the environment its engine and tools
    inherit, naming the session's secret and the bundle the render binds
    (``sandbox_render.EXIT_BUNDLE_PATH``), kept for the processes the proxy
    spawns later for the session (``env_of``). Nothing is attached when the
    one machine rule refuses the machine (``exit_machine_refusal``): the
    build has left out every tool that holds a declared key there, so its
    render gets the description without the exit. A session with no
    machine has no exit: every session runs on one, and such a start is
    logged."""
    from core.sandbox.sandbox_render import EXIT_BUNDLE_PATH
    from services.mcp.mcp_session_config import exit_machine_refusal
    if machine is None:
        logger.warning("credential exit: %s starts on no machine, so no exit is attached", session_id[:8])
        return
    if exit_machine_refusal(machine):
        return
    carves = tuple(str(h) for h in getattr(builder.cfg, "net_allow_hosts", ()) or ())
    held = _machine_session(session_id, carves)
    held.compute = True
    builder.exit_port = _sandbox_exit_port(builder)
    env = _relay_env(port=builder.exit_port, credential=held.secret,
                     bundle_path=EXIT_BUNDLE_PATH, allow_hosts=carves, direct_hosts=direct_hosts,
                     upstream=bool(getattr(machine, "exit_upstream", False)))
    held.env = dict(env)
    extra_env.update(env)


def _sandbox_exit_port(builder) -> int:
    """The port inside a compute session's namespace its exit is spliced
    onto: ``_SANDBOX_EXIT_PORT`` unless the description already forwards
    that port or names it as the platform's (``config.PORT``), then the
    next one free of both."""
    taken = {int(config.PORT or 0)}
    for forward in getattr(builder.cfg, "net_forwards", ()) or ():
        head = str(forward).split(":", 1)[0]
        if head.isdigit():
            taken.add(int(head))
    port = _SANDBOX_EXIT_PORT
    while port in taken:
        port += 1
    return port


def _door_secret(session_id: str) -> str:
    """The secret a machine's exit admits the session by: derived from the
    platform's own key and the session id, so a session taken back after a
    proxy restart is admitted again by the door its machine kept (the tools'
    environments and git's entries carry it), and nothing of it is stored."""
    from auth import tokens
    return tokens.derive(tokens.EXIT_DOOR, session_id)[:32]


def _machine_session(session_id: str, carves: Iterable[str] = ()) -> _Session:
    """The session's record for a machine's exit, with its door's secret (a
    second build of the same id, a terminal re-opened or a session taken
    back after a restart, gets the same one)."""
    register(session_id, carves)
    held = _sessions[session_id]
    if not held.secret:
        held.secret = _door_secret(session_id)
    return held


def machine_env(session_id: str, placement) -> dict[str, str]:
    """A paired computer's variables for the processes that hold a
    placeholder (the declared tools' bundles, git's entries, gh's process):
    the machine's exit on its loopback with the session's secret, the trust
    variables naming the machine's bundle under the OtoDock folder it
    reports (``exit_relay.BUNDLE_RELPATH``), the loopback names and the
    engines' hosts direct. Empty for a compute machine (its variables are
    the sandbox's) and when the one machine rule refuses the machine."""
    from core import placement as _placement
    from services.mcp.mcp_session_config import exit_machine_refusal
    if not session_id or placement is None or not placement.is_remote:
        return {}
    if placement.kind == _placement.KIND_COMPUTE or exit_machine_refusal(placement):
        return {}
    held = _machine_session(session_id)
    folder = str(placement.reported_otodock_dir).rstrip("/")
    env = _relay_env(port=int(placement.exit_port), credential=held.secret,
                     bundle_path=f"{folder}/{exit_relay.BUNDLE_RELPATH}",
                     upstream=bool(getattr(placement, "exit_upstream", False)))
    held.env = dict(env)
    return env


def machine_secret(session_id: str) -> str:
    """The secret a machine's exit admits the session by, "" for a session
    with no machine exit."""
    held = _sessions.get(session_id)
    return held.secret if held is not None else ""


def cli_process_env(session_id: str) -> dict[str, str]:
    """The variables gh's process gets on a paired computer (the helper
    sets them on the real command), so gh reaches GitHub through the
    machine's exit with its placeholder; empty elsewhere, where the
    session's environment carries them already."""
    held = _sessions.get(session_id)
    if held is None or not held.secret or held.compute:
        return {}
    return dict(held.env)


def admitted_hosts(cred: mcp_gateway.ExitCredential) -> tuple[str, ...]:
    """The declared hosts the value may go to now: a command-line
    credential's all (the helper judges each command), every other
    credential's as the bearer allowlist admits them under the manifest's
    name (``mcp_gateway.resolve_credential``'s own check, asked ahead of a
    machine's push)."""
    if cred.cli is not None:
        return cred.hosts
    return tuple(h for h in cred.hosts if mcp_gateway._host_allowed(cred.allowlist_key, h))


def probe_host(cred: mcp_gateway.ExitCredential) -> str:
    """The host a value is resolved toward when no request names one (the
    push registry's pushes): the first admitted literal host, else a label
    under the first admitted wildcard's domain, which the allowlist's
    pattern and the entry's hosts both admit; "" when none is admitted."""
    hosts = admitted_hosts(cred)
    for host in hosts:
        if not host.startswith("*."):
            return host
    return f"exit{hosts[0][1:]}" if hosts else ""


def machine_block(session_id: str, bundles, token_hash: str) -> dict | None:
    """The ``exit`` block of a machine session's start payload (and of a
    Direct tool's open on compute): the session's secret, the hash the
    push registry keys the session's values by (``mcp_gateway_push``), and
    its entries (placeholder, header, prefix, key, the admitted hosts), with
    the session's carves for a compute machine's dial rule. Nothing of a
    value. None when the session has no machine exit, or on a paired
    computer when none of its tools holds a placeholder."""
    held = _sessions.get(session_id)
    if held is None or not held.secret:
        return None
    entries: list[dict] = []
    minted = False
    for bundle in (bundles or {}).values():
        for cred in getattr(bundle, "exit", ()) or ():
            minted = True
            hosts = admitted_hosts(cred)
            if not hosts:
                continue
            entry = {"placeholder": cred.placeholder, "header": cred.header, "prefix": cred.prefix,
                     "key": cred.key, "hosts": list(hosts)}
            # A machine's exit passes a large body as it arrives for an entry
            # that streams, and asks the platform about each request of a
            # judged one (``judge_request``).
            if cred.streams:
                entry["streams"] = True
            if cred.judged:
                entry["judged"] = True
            entries.append(entry)
    # A compute sandbox names the exit for every process, and a paired tool
    # that holds a placeholder names it too: each gets its door, and a door
    # with no entry is a plain tunnel for a session that holds no
    # placeholder.
    if not entries and not held.compute and not minted:
        return None
    return {"secret": held.secret, "token_hash": token_hash, "entries": entries, "carves": sorted(held.carves)}


def env_of(session_id: str) -> dict[str, str]:
    """The exit's variables of a session ``attach_sandbox`` prepared, for a
    process the proxy spawns for it after the start (the direct layer's
    tool children); empty for every other session."""
    held = _sessions.get(session_id)
    return dict(held.env) if held is not None else {}


def register(session_id: str, allow_hosts: Iterable[str]) -> None:
    """The session's carves (``SandboxDescription.allow_hosts``): the private
    addresses a compute machine's exit may dial for it, as the namespace may
    (they ride the door's ``carves``)."""
    carves = frozenset(str(h) for h in allow_hosts if h)
    held = _sessions.get(session_id)
    if held is None:
        _sessions[session_id] = _Session(carves=carves)
    else:
        held.carves = carves


def forget(session_id: str) -> None:
    """The session's purge: its record goes."""
    _sessions.pop(session_id, None)


_hook_registered = False


def register_purge_hook() -> None:
    """The broker's purge forgets the session here too (once per process)."""
    global _hook_registered
    if _hook_registered:
        return
    _hook_registered = True
    mcp_gateway.add_purge_hook(forget)


def retire_platform_authority() -> int:
    """At boot: remove the platform's own exit authority an earlier release
    made under ``<PLATFORM_DATA_DIR>/credential-exit/`` (the machines' exits
    keep their own): its two files (``exit_authority.KEY_NAME``,
    ``CERT_NAME``), a leaf's key or certificate a stopped make left in its
    leaves folder (``exit_authority.LEAVES_DIRNAME``), and each folder once
    nothing else is left in it; a link at either folder is left alone, with
    a warning. How many files went. Blocking."""
    folder = Path(config.PLATFORM_DATA_DIR) / "credential-exit"
    leaves = folder / "leaves"
    for place in (folder, leaves):
        if os.path.islink(place):
            # Never followed: what a link points at is not the authority's.
            logger.warning("credential exit: %s is a link, so the old authority there stays", place.name)
            return 0
    doomed = [folder / "ca.key", folder / "ca.pem"]
    with contextlib.suppress(OSError):
        doomed += [leaves / e.name for e in os.scandir(leaves)
                   if e.is_file(follow_symlinks=False) and e.name.endswith((".key", ".pem"))]
    removed = 0
    for path in doomed:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning("credential exit: the platform's old authority file %s stays: %s", path.name, exc)
    for empty in (leaves, folder):
        with contextlib.suppress(OSError):
            empty.rmdir()
    if removed:
        logger.info("credential exit: removed the platform's own authority of an earlier release")
    return removed


def exit_resolution(cred: mcp_gateway.ExitCredential, host: str) -> tuple[str, float | None]:
    """The value behind ``cred`` toward ``host`` now, the prefix the tool
    writes itself stripped, and the seconds its token file gives it (None
    for a static value or a command-line credential): the command-line
    credential's token, or the gateway's resolution (the allowlist's answer
    for the host, the egress guard, the value or the token file). The push
    registry's pushes read it. Blocking; raises ``Judged``."""
    if cred.cli is not None:
        cli = mcp_gateway.resolve_cli_credential(cred.cli)
        if isinstance(cli, mcp_gateway.Refusal):
            raise Judged(403, cli.detail)
        return cli.secret, None
    res = mcp_gateway.resolve_credential(cred.gateway_for(host))
    if isinstance(res, mcp_gateway.Refusal):
        raise Judged(403, res.detail)
    value = res.value[len(cred.prefix):] if res.value.startswith(cred.prefix) else res.value
    return value, res.expires_in


#: How a refusal is sent, by the provider's word for its caller.
_REFUSAL_TYPES = {"api": "application/json; charset=utf-8", "lfs": "application/vnd.git-lfs+json"}
REFUSAL_NOT_HELD = "This session holds no such credential"


def judge_request(session_id: str, placeholder: str, method: str, host: str, target: str,
                  about: dict[str, str], body: bytes | None) -> exit_relay.Judgement:
    """The platform's word on one request a session's exit is about to send
    with the credential behind ``placeholder``: the provider's own rule for
    a command-line credential whose reach narrows
    (``auth/github_reach.judge_request``, the reach the session was built
    with), and "go" for one that does not. A machine's exit asks here
    through the command-line credential's route, on the loop: the lookup walks the broker's store, which the loop alone
    changes, and the rule reads a small body at most
    (``github_reach.REQUEST_BODY_MAX``)."""
    from auth import github_reach
    cred = mcp_broker.exit_credential(session_id, placeholder)
    if cred is None or cred.cli is None:
        return exit_relay.Judgement(status=403, detail=REFUSAL_NOT_HELD)
    reach = github_reach.Reach.from_dict(cred.cli.reach)
    if reach is None or not reach.narrows:
        return exit_relay.Judgement()
    verdict = github_reach.judge_request(
        reach, method, host, target, content_type=str(about.get("content_type") or ""),
        content_encoding=str(about.get("content_encoding") or ""), body=body)
    if verdict.refusal:
        return exit_relay.Judgement(status=403, detail=verdict.refusal,
                                    content_type=_REFUSAL_TYPES.get(verdict.kind, ""))
    return exit_relay.Judgement(wants_body=verdict.wants_body, body=verdict.body, link=verdict.link)


__all__ = [
    "ENGINE_HOSTS", "ENV_NAMES", "MAX_BODY_BYTES", "PLACEHOLDER_PREFIX", "REFUSAL_HOST",
    "REFUSAL_PLACE", "REFUSAL_PLAIN", "REFUSAL_WHERE", "Judged", "admitted_hosts", "attach_sandbox",
    "cli_process_env", "env_of", "exit_resolution", "forget",
    "judge_request", "machine_block", "machine_env", "machine_secret", "new_placeholder", "own_direct",
    "probe_host", "register", "register_purge_hook", "retire_platform_authority",
]
