"""A session's credential exit on this machine: the proxy's relay run here.

The platform's credential exit (``proxy/core/credentials/credential_exit.py``)
lets a session hold a placeholder in place of a tool's key and adds the value
on the way out. On a paired computer and on a compute machine the same relay
(``_vendored/exit_relay.py``, byte for byte the proxy's) listens on this
machine's loopback, with this module as its backend:

* **The listener** starts with the satellite, on every machine kind, on a
  port the OS picks, and the capabilities and every heartbeat carry it
  (``exit_port``, ``exit_ok``). Nothing else exists until a session needs
  it: the authority (``_vendored/exit_authority.py``) and the trust bundle
  under ``<otodock dir>/credential-exit/`` are made at the first door. A
  machine whose venv lacks the relay's packages (or still installs them
  after an update) runs no exit and says why (``exit_why``); the platform
  then leaves out of its sessions every tool that needs a key.
* **A door per session** comes with the start payload's ``exit`` block: the
  session's secret (what its ``HTTPS_PROXY`` carries as the Basic proxy
  credential), the hash of its own token (the key the platform's pushes
  use) and its entries (placeholder, header, prefix, key, hosts). A second
  start for the same id replaces the door (a terminal re-opened). Nothing
  of a value is in the block.
* **The values** are what the platform pushed into the credential gateway's
  table (``transport/mcp_gateway.GatewayTable``, the ``mcp_gateway_token``
  frames keyed by the session's token hash and the placeholder, renewed and
  revoked by the platform's push registry, wiped at the purge): the relay
  reads the table per request and holds a value no longer than the
  request. Nothing here fetches a value.
* **Where it dials.** On a compute machine the exit runs outside the
  sandbox's namespace, so it applies the sandbox shim's own rule
  (``_vendored/outbound_url.sandbox_dial_refused`` with the session's
  carves) and pins the forward to a checked address; on a paired computer
  every address passes and the forward names the host, since there is no
  sandbox there.
* **Through the machine's own proxy.** Where this machine reaches the
  world through an outgoing proxy (its own settings, or for the install's
  own compute machine the platform's, sent at the sign-in:
  ``set_platform_egress``), the relay sends its tunnels and forwards
  through it, a target its bypass names going direct. On a compute machine
  the target is still judged by the sandbox's rule before the proxy is
  asked (``upstream_admits``). The proxy and its credential live in memory,
  never in this process's environment or a log; a SOCKS proxy turns the
  exit off with its reason.
* **A judged entry.** An entry the platform marks ``judged`` (git's and
  gh's, under a narrowed reach) has each of its requests put to the platform
  before the value is added: the relay asks this backend, which posts the
  request's method, host and target to the command-line credential's route
  over the session's own loopback tunnel, with the session's command-line
  token (``_ask``). Only an answer that is a verdict lets a request go: any
  other answer, a failed or slow ask, or a door with no token refuses it.
  An entry marked ``streams`` passes a large body as it arrives.
* **The heal.** CPython's Windows event loop closes a listening socket for
  good after one failed accept: the loop handler (``transport/http_tunnel
  .make_loop_exception_handler``) names this listener's socket to
  ``heal_soon``, which binds the same port again (the sessions' environments
  name it) and reports ``exit_ok`` false meanwhile.

Admission is the session's secret, compared in constant time. A loopback
port is open to every process of this machine's account; the secret sits in
the environment of the processes that hold a placeholder (SECURITY-DECISIONS
decision 34 on the platform).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import importlib
import ipaddress
import logging
import socket
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .._vendored import loop_watchdog
from ..background import spawn
from ..config import otodock_dir

logger = logging.getLogger("satellite.credential-exit")

AUTHORITY_DIRNAME = "credential-exit"
BUNDLE_NAME = "bundle.pem"
#: How long a request waits for a push in flight (the gateway's own grace).
_PUSH_WAIT_S = 3.0
_CONNECT_TIMEOUT_S = 10.0
REFUSAL_NO_VALUE = (
    "No credential has reached this computer for this session and tool. The platform sends it "
    "when the session starts and renews it while the session runs."
)
REFUSAL_NO_WORD = "OtoDock could not check this request. Try again."
#: The platform route that judges a judged entry's requests: fixed here,
#: never named by a payload, since the session's token is posted to it.
JUDGE_PATH = "/v1/hooks/cli-credential"
#: One ask's whole time, the wait for a free slot included.
_JUDGE_TIMEOUT_S = 10.0
#: The asks one door runs at a time: a session's flood of requests waits
#: here, never in the machine's shared hook streams.
_JUDGE_ASKS = 8
_CLI_TOKEN_ENV = "OTO_CLI_TOKEN"


@dataclass(frozen=True)
class _Entry:
    placeholder: str
    header: str
    prefix: str
    key: str
    hosts: tuple[str, ...]
    streams: bool = False
    judged: bool = False


@dataclass
class _Door:
    session_id: str
    secret: str = field(repr=False)
    token_hash: str
    entries: dict[str, _Entry]
    carves: frozenset[str]
    client: Any = None
    # The forward's clients through the machine's own proxy, by its address:
    # the credential each was made with, and the client.
    proxied: dict[str, tuple[str, Any]] = field(default_factory=dict, repr=False)
    # Where and with what a judged entry's requests are put to the platform:
    # the session's loopback tunnel and its command-line token ("" when the
    # payload carried none: every judged request is then refused).
    platform_url: str = ""
    cli_token: str = field(default="", repr=False)
    asks: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(_JUDGE_ASKS))
    # The forward's certificate check, built when the door opened (a worker
    # thread: a system's store can take a while to read, Windows' above all).
    verify: Any = field(default=None, repr=False)


@dataclass
class _State:
    relay: Any = None
    authority: Any = None
    table: Any = None
    ok: bool = False
    compute: bool = False
    onlink: tuple = ()
    loop: asyncio.AbstractEventLoop | None = None
    heal_task: asyncio.Task | None = None
    doors: dict[str, _Door] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # The client the asks go through: this machine's own tunnel, so it reads
    # no proxy variable.
    judge_client: Any = None
    said_no_exit: bool = False
    install_tried: bool = False
    # Why this machine runs no exit right now ("" while it does): the
    # capabilities and every heartbeat carry it (``state``).
    why: str = ""
    # Called once the listener is up after its packages were installed, so
    # the platform learns of it at once (the link's ``heartbeat_now``).
    on_ready: Any = None
    # The install's own compute machine: the platform's outgoing proxy and
    # its ``NO_PROXY`` (``set_platform_egress``), in memory only, never in
    # this process's environment; the platform's trust files as PEM text,
    # added to the forward's trust and to the bundle the sessions read.
    platform_egress: dict | None = field(default=None, repr=False)
    extra_trust: str = ""
    # The upstream's source and bypass answers, read again after a few
    # seconds (``_UPSTREAM_TTL_S``), and the names the resolver could not
    # answer lately (``_UNRESOLVED_TTL_S``).
    proxies: tuple[float, dict] = field(default=(0.0, {}), repr=False)
    bypass: dict[str, tuple[float, bool]] = field(default_factory=dict)
    unresolved: dict[str, float] = field(default_factory=dict)


_state = _State()
#: The upstream verification of the forward: the system authorities (httpx's
#: default); a test points it at its own authority's file.
_upstream_verify: Any = True
# The leaf makes, on a thread of their own: the authority makes one at a time
# anyway, so the asks that wait behind a stalled make hold this thread alone,
# never the default executor's that every other ``to_thread`` here shares.
_maker: ThreadPoolExecutor | None = None


def _make_pool() -> ThreadPoolExecutor:
    """The exit's one thread for its leaf makes, made at the first, reported
    to the loop's watchdog."""
    global _maker
    if _maker is None:
        _maker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="credential-exit")
        loop_watchdog.watch_executor("credential-exit", _maker)
    return _maker


#: How long the machine's proxy settings and one host's bypass answer are
#: kept, and how long a name the resolver did not answer skips its lookup.
_UPSTREAM_TTL_S = 5.0
_UNRESOLVED_TTL_S = 30.0
#: The bound on the best-effort lookup of a target the proxy will resolve.
_LOOKUP_S = 2.0


def set_platform_egress(block: Any, *, compute: bool) -> None:
    """The ``egress_proxy`` of the platform's ``auth_result``: taken by a
    compute machine alone (the install's own: the platform sends it to no
    other), checked, kept in memory, never logged. None or junk clears it."""
    egress: dict | None = None
    trust: list[str] = []
    if compute and isinstance(block, dict) and isinstance(block.get("https"), str) and block["https"].strip():
        no_proxy = block.get("no_proxy")
        egress = {"https": block["https"].strip(), "no_proxy": no_proxy if isinstance(no_proxy, str) else ""}
        items: list[Any] = []
        for key in ("trust", "cert_dir"):
            if isinstance(block.get(key), list):
                items.extend(block[key])
        for item in items:
            data = item.get("data_b64") if isinstance(item, dict) else None
            if not isinstance(data, str):
                continue
            try:
                text = base64.b64decode(data, validate=True).decode("ascii")
            except (ValueError, UnicodeDecodeError):
                continue
            if "-----BEGIN CERTIFICATE-----" in text:
                trust.append(text.strip())
    _state.platform_egress = egress
    _state.extra_trust = "\n".join(trust)
    _state.proxies = (0.0, {})
    _state.bypass.clear()
    _refresh_upstream_why()


def _proxies() -> dict:
    """This machine's proxy settings (``urllib.request.getproxies``: the
    environment and, on Windows and macOS, the system's), kept a few
    seconds."""
    at, proxies = _state.proxies
    now = time.monotonic()
    if now - at > _UPSTREAM_TTL_S:
        proxies = dict(urllib.request.getproxies())
        _state.proxies = (now, proxies)
    return proxies


def _upstream_url() -> str:
    """The outgoing proxy an https target goes through: the platform's for
    the install's own compute machine, else this machine's own for https,
    else for all; "" for none."""
    if _state.platform_egress is not None:
        return str(_state.platform_egress.get("https") or "")
    proxies = _proxies()
    return str(proxies.get("https") or proxies.get("all") or "")


async def _bypassed(host: str) -> bool:
    """Whether ``host`` goes around the proxy: the platform's ``NO_PROXY``
    for the install's own compute machine, this machine's own bypass list
    otherwise (``urllib.request.proxy_bypass``: the environment's, and on
    Windows and macOS the system's, which may resolve names: off the loop).
    One parser, urllib's; an answer kept a few seconds."""
    name = host.lower().rstrip(".")
    now = time.monotonic()
    held = _state.bypass.get(name)
    if held is not None and now - held[0] <= _UPSTREAM_TTL_S:
        return held[1]
    if _state.platform_egress is not None:
        no_proxy = str(_state.platform_egress.get("no_proxy") or "")
        answer = bool(no_proxy) and bool(urllib.request.proxy_bypass_environment(name, {"no": no_proxy}))
    else:
        # Within the lookup's bound: an answer that does not come sends the
        # name through the proxy, the machine's own way out.
        try:
            answer = bool(await asyncio.wait_for(asyncio.to_thread(urllib.request.proxy_bypass, name), _LOOKUP_S))
        except (asyncio.TimeoutError, TimeoutError):
            answer = False
    _state.bypass[name] = (now, answer)
    return answer


def _verify() -> Any:
    """The forward's certificate check, on the base the sessions' own bundle
    is written from (``exit_authority.system_bundle_text``: the system's
    store, else certifi's), with the platform's trust files beside it on the
    install's own compute machine behind a proxy that inspects TLS; a test's
    own file when it set one."""
    if _upstream_verify is not True:
        return _upstream_verify
    import ssl

    from .._vendored import exit_authority
    pems = exit_authority.system_bundle_text()
    if _state.extra_trust:
        pems = f"{pems}\n{_state.extra_trust}"
    # ``cadata`` takes ASCII text alone, and a store's comment lines may name
    # a root outside it; the certificates themselves are base64.
    return ssl.create_default_context(cadata=pems.encode("ascii", "ignore").decode("ascii"))


def _refresh_upstream_why() -> None:
    """A SOCKS proxy (the one kind the relay does not speak) turns the exit
    off with its reason; any other proxy, or none, leaves it as it is. The
    sign-in calls it: without the exit's packages it returns, and the
    reason they gave stands."""
    try:
        from .._vendored import exit_relay
    except ImportError:
        return
    try:
        exit_relay.parse_upstream(_upstream_url())
    except exit_relay.UpstreamRefused as refused:
        if refused.kind == "socks":
            if _state.relay is not None:
                _state.ok = False
            _state.why = "socks"
            return
    except Exception:  # noqa: BLE001  # no relay package: the import's own reason stands
        return
    # Any other address the relay does not speak is dialled around
    # (``_Backend.upstream``): only a SOCKS proxy keeps the exit off.
    if _state.why == "socks" and _state.relay is not None:
        _state.ok, _state.why = True, ""


def upstream_on() -> bool:
    """Whether this machine's exit reaches the world through an outgoing
    proxy it speaks (the ``exit_upstream`` the platform reads)."""
    try:
        from .._vendored import exit_relay
        return exit_relay.parse_upstream(_upstream_url()) is not None
    except Exception:  # noqa: BLE001  # a SOCKS proxy or no relay package: no upstream
        return False


async def start_listener(table: Any, *, compute: bool, on_ready: Any = None) -> int:
    """Listen on the loopback for the sessions' exit; the port, or 0 when
    this machine runs no exit (the packages missing or still installing, the
    bind refused), each said once in the log and as ``exit_why``."""
    if on_ready is not None:
        _state.on_ready = on_ready
    try:
        from .._vendored import exit_relay, outbound_url
    except ImportError as exc:
        if compute or _state.install_tried:
            logger.warning("credential exit: not started, a package is missing from the venv: %s", exc)
            _state.why = "packages"
            return 0
        # The update to this version ran the previous one's code, which
        # knows only requirements.txt: the exit's own lock goes in now, in
        # the background, so the boot and the update's guard are never held.
        _state.install_tried = True
        _state.why = "installing"
        logger.info("credential exit: a package is missing from the venv (%s): installing the exit's packages", exc)
        spawn(_install_then_listen(table), name="install", owner="credential_exit")
        return 0
    relay = exit_relay.Relay(_Backend(), logger=logger)
    try:
        port = await relay.start()
    except OSError as exc:
        logger.warning("credential exit: not started, the loopback port could not be bound: %s", exc)
        _state.why = "bind"
        return 0
    _state.relay = relay
    _state.table = table
    _state.compute = compute
    _state.onlink = await asyncio.to_thread(outbound_url.read_onlink) if compute else ()
    _state.loop = asyncio.get_running_loop()
    _state.ok = True
    _state.why = ""
    _refresh_upstream_why()
    logger.info("credential exit on 127.0.0.1:%s%s", port,
                ", through this machine's proxy" if upstream_on() else "")
    if _state.why == "socks":
        logger.warning("credential exit: this machine's proxy is a SOCKS proxy, which the exit cannot use: the "
                       "tools that need a key stay off here")
    return port


def _install_exit_lock() -> bool:
    """The exit's lock (``requirements-exit.txt``: beside the package in an
    installed satellite, inside it in a checkout) into this venv, best
    effort and from wheels alone (``lifecycle_update.install_exit_packages``).
    Blocking."""
    from ..transport.lifecycle_update import EXIT_REQUIREMENTS, install_exit_packages
    here = Path(__file__).resolve().parents
    for folder in (here[2], here[1]):
        if (folder / EXIT_REQUIREMENTS).is_file():
            return install_exit_packages(sys.executable, folder)
    return False


async def _install_then_listen(table: Any) -> None:
    if not await asyncio.to_thread(_install_exit_lock):
        logger.warning("credential exit: not started, its packages could not be installed: the tools that need a "
                       "key stay off on this computer until they are")
        _state.why = "packages"
        await _tell_platform()
        return
    importlib.invalidate_caches()
    await start_listener(table, compute=False)
    await _tell_platform()


async def _tell_platform() -> None:
    """A heartbeat out of turn, so the platform reads the exit's new state
    before its next session here: never raises."""
    ready = _state.on_ready
    if ready is None:
        return
    with contextlib.suppress(Exception):
        await ready()


def takes_gateway_frame(frame_type: str, msg: dict) -> bool:
    """Whether a compute machine takes this platform frame: any frame but
    the gateway's; of those, a wipe (the session's entries go, whatever their
    kind) and a token keyed by an exit placeholder. A gateway lease by MCP
    name is never held on a compute machine (its sessions' gateway requests
    ride the tunnel to the platform's gateway), and without the exit's
    modules no token is taken."""
    if frame_type != "mcp_gateway_token":
        return True
    try:
        from .._vendored import exit_relay
    except ImportError:
        return False
    return exit_relay.PLACEHOLDER_RE.fullmatch(str(msg.get("mcp") or "").encode()) is not None


async def stop_listener() -> None:
    """End the listener and every door (the satellite stops)."""
    relay, _state.relay = _state.relay, None
    _state.ok = False
    with _state.lock:
        doors = list(_state.doors.values())
        _state.doors.clear()
    for door in doors:
        await _close_client(door)
    judge_client, _state.judge_client = _state.judge_client, None
    if judge_client is not None:
        with contextlib.suppress(Exception):
            await judge_client.aclose()
    if relay is not None:
        await relay.stop()


def port() -> int:
    return int(_state.relay.port) if _state.relay is not None and _state.ok else 0


def state() -> dict:
    """What the capabilities and the heartbeat carry: the port, whether the
    listener answers and why not (``exit_why``, "" while it does). With no
    listener, the reason alone (nothing when none is known)."""
    if _state.relay is None:
        return {"exit_ok": False, "exit_why": _state.why} if _state.why else {}
    return {"exit_port": int(_state.relay.port), "exit_ok": bool(_state.ok),
            "exit_why": "" if _state.ok else (_state.why or "bind"), "exit_upstream": upstream_on()}


def bundle_path() -> Path:
    """Where this machine's trust bundle lives: the proxy composes the same
    path from the OtoDock folder the satellite reports."""
    return otodock_dir() / AUTHORITY_DIRNAME / BUNDLE_NAME


def render_values() -> tuple[int, str]:
    """``(exit_target_port, exit_bundle)`` for a compute machine's render:
    the listener's port and the bundle's path while both stand, else
    ``(0, "")`` so the render refuses a description that names an exit."""
    path = bundle_path()
    if not _state.ok or _state.relay is None or not path.is_file():
        return 0, ""
    return int(_state.relay.port), str(path)


def owns_socket(sock: object) -> bool:
    return _state.relay is not None and _state.relay.owns_socket(sock)


def heal_soon(reason: str) -> None:
    """Bind the same port again after the listening socket stopped
    accepting; one heal at a time, on the loop that serves the relay."""
    loop = _state.loop
    if loop is None or _state.relay is None:
        return
    if _state.heal_task is not None and not _state.heal_task.done():
        return
    _state.ok = False
    logger.warning("credential exit: the listener stopped accepting (%s); binding its port again", reason)
    _state.heal_task = loop.create_task(_heal())


async def _heal() -> None:
    relay = _state.relay
    if relay is None:
        return
    ok = await relay.rebind()
    _state.ok = ok
    if ok:
        logger.info("credential exit: listening again on 127.0.0.1:%s", relay.port)
    else:
        logger.warning("credential exit: the port could not be bound again; the tools that need a key stay off "
                       "here until it is")


def start(session_id: str, config: dict) -> None:
    """The session's door from the start payload's ``exit`` block (nothing
    without one), the authority and the bundle made at the first; a block
    for an id that has a door replaces it. Never raises: a session whose
    door cannot open starts, and its calls through the exit are refused."""
    block = config.get("exit")
    if not isinstance(block, dict):
        return
    sid = session_id[:8]
    if _state.relay is None:
        if not _state.said_no_exit:
            _state.said_no_exit = True
            logger.warning("credential exit: %s: a session carries an exit block, but this machine runs no exit",
                           sid)
        return
    secret = str(block.get("secret") or "")
    token_hash = str(block.get("token_hash") or "")
    entries = _entries_of(block.get("entries"))
    if not secret or not token_hash:
        logger.warning("credential exit: %s: the exit block names no secret or token hash; no door", sid)
        return
    try:
        _ensure_authority()
    except Exception as exc:  # noqa: BLE001  # a door with no bundle is no door; the session still starts
        logger.warning("credential exit: %s: the authority could not be made: %s", sid, exc)
        return
    carves = frozenset(str(h) for h in (block.get("carves") or []) if h)
    platform_url, cli_token = _platform_url(config), str((config.get("env") or {}).get(_CLI_TOKEN_ENV) or "")
    try:
        verify = _verify() if _upstream_verify is True else None
    except Exception as exc:  # noqa: BLE001  # the clients build it themselves then
        logger.warning("credential exit: %s: the forward's trust was not read at the start: %s", sid, exc)
        verify = None
    with _state.lock:
        old = _state.doors.get(session_id)
        if old is not None and old.secret == secret and old.token_hash == token_hash:
            # The same session's block again (Direct LLM opens it once per
            # tool): the door is updated in place and keeps its pool, so a
            # tool opened mid-run cuts no request another tool has in flight.
            # A payload that names no tunnel or no token (a tool's open)
            # never clears the ones the session's start gave.
            old.entries, old.carves = entries, carves
            old.platform_url, old.cli_token = platform_url or old.platform_url, cli_token or old.cli_token
            old.verify = old.verify or verify
            logger.info("credential exit: %s: %d placeholder(s), updated", sid, len(entries))
            return
        _state.doors[session_id] = _Door(session_id=session_id, secret=secret, token_hash=token_hash,
                                         entries=entries, carves=carves, platform_url=platform_url,
                                         cli_token=cli_token, verify=verify)
    if old is not None:
        _close_client_soon(old)
    logger.info("credential exit: %s: %d placeholder(s)%s", sid, len(entries), ", replaced" if old else "")


def stop(session_id: str) -> None:
    """Drop the session's door; nothing for a session this module never saw."""
    with _state.lock:
        door = _state.doors.pop(session_id, None)
    if door is not None:
        _close_client_soon(door)


def _entries_of(raw: object) -> dict[str, _Entry]:
    out: dict[str, _Entry] = {}
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        placeholder = str(item.get("placeholder") or "")
        header = str(item.get("header") or "")
        hosts = tuple(str(h) for h in (item.get("hosts") or []) if h)
        if not placeholder or not header or not hosts:
            continue
        out[placeholder] = _Entry(placeholder=placeholder, header=header, prefix=str(item.get("prefix") or ""),
                                  key=str(item.get("key") or ""), hosts=hosts,
                                  streams=item.get("streams") is True, judged=item.get("judged") is True)
    return out


def _platform_url(config: dict) -> str:
    """Where this machine reaches the platform for the session: its own
    link port for a compute session, else the payload's ``PROXY_URL`` (this
    machine's loopback tunnel), as the SSH agent socket posts."""
    port = int(config.get("tunnel_port") or 0)
    if port > 0:
        return f"http://127.0.0.1:{port}"
    return str((config.get("env") or {}).get("PROXY_URL") or "")


def _word_of(answer: Any):
    """The platform's verdict as the relay's ``Judgement``, None for an
    answer that is none: a "go" is read from a verdict alone."""
    from .._vendored import exit_relay
    verdict = answer.get("verdict") if isinstance(answer, dict) else None
    if not isinstance(verdict, dict):
        return None
    status, wants, detail = verdict.get("status"), verdict.get("wants_body"), verdict.get("detail")
    content_type, body_b64, link = verdict.get("content_type"), verdict.get("body_b64"), verdict.get("link")
    if (type(status) is not int or type(wants) is not int or status < 0 or wants < 0
            or not isinstance(detail, str) or not isinstance(content_type, str)):
        return None
    if status and not 400 <= status <= 599:
        return None
    body = None
    if body_b64 is not None:
        if not isinstance(body_b64, str):
            return None
        try:
            body = base64.b64decode(body_b64, validate=True)
        except ValueError:
            return None
    pair = None
    if link is not None:
        if not (isinstance(link, list) and len(link) == 2 and all(isinstance(x, str) for x in link)):
            return None
        pair = (link[0], link[1])
    return exit_relay.Judgement(status=status, detail=detail, content_type=content_type, wants_body=wants,
                                body=body, link=pair)


async def _ask(door: _Door, payload: dict):
    """One ask of the platform about a judged entry's request; its word, or
    None when no verdict came (any other status, any other body)."""
    import httpx
    if _state.judge_client is None:
        _state.judge_client = httpx.AsyncClient(trust_env=False, timeout=_JUDGE_TIMEOUT_S)
    async with door.asks:
        resp = await _state.judge_client.post(
            door.platform_url.rstrip("/") + JUDGE_PATH, json=payload,
            headers={"Authorization": f"Bearer {door.cli_token}",
                     "X-Tunnel-Timeout-S": str(int(_JUDGE_TIMEOUT_S))})
    if resp.status_code != 200:
        return None
    return _word_of(resp.json())


def _ensure_authority() -> None:
    """The authority under this machine's OtoDock folder and the bundle a
    session's trust variables name, made at the first door and compared at
    every door (blocking: the doors open on worker threads). One instance,
    whichever door comes first. The authority's own lock makes the key once,
    and two doors at once each rename a whole bundle into place."""
    # The first import loads ``cryptography`` from disk: never under the
    # lock the loop takes at every request.
    from .._vendored import exit_authority
    with _state.lock:
        if _state.authority is None:
            _state.authority = exit_authority.ExitAuthority(otodock_dir() / AUTHORITY_DIRNAME)
        authority = _state.authority
    authority.write_bundle(bundle_path(), _state.extra_trust)


def _new_leaf(host: str):
    """A leaf for ``host`` (blocking, on the exit's own thread): the one an
    earlier ask made while this one waited, as it is, else a new one with
    the bundle compared first."""
    authority = _state.authority
    cached = authority.cached_context(host) if authority is not None else None
    if cached is not None:
        return cached
    _ensure_authority()
    return _state.authority.leaf_context(host)


def _close_client_soon(door: _Door) -> None:
    loop = _state.loop
    if (door.client is None and not door.proxied) or loop is None:
        return
    with contextlib.suppress(RuntimeError):
        loop.call_soon_threadsafe(lambda: loop.create_task(_close_client(door)))


async def _close_client(door: _Door) -> None:
    clients = [door.client, *(client for _credential, client in door.proxied.values())]
    door.client = None
    door.proxied = {}
    for client in clients:
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()


def _door_of(sid: str) -> _Door | None:
    with _state.lock:
        return _state.doors.get(sid)


class _Backend:
    """This machine's answers to the relay."""

    def session_of(self, headers) -> str | None:
        from .._vendored import exit_relay
        parsed = exit_relay.basic_credential(headers)
        if parsed is None or parsed[0] not in exit_relay.PROXY_USERS:
            return None
        with _state.lock:
            doors = list(_state.doors.values())
        for door in doors:
            if hmac.compare_digest(parsed[1].encode("utf-8"), door.secret.encode("utf-8")):
                return door.session_id
        return None

    def credential(self, sid: str, placeholder: str):
        door = _door_of(sid)
        return door.entries.get(placeholder) if door is not None else None

    def takes_host(self, sid: str, host: str) -> bool:
        from .._vendored import exit_relay
        door = _door_of(sid)
        return door is not None and any(exit_relay.host_matches(e.hosts, host) for e in door.entries.values())

    async def value_of(self, sid: str, entry, host: str) -> str:
        from .._vendored import exit_relay
        door = _door_of(sid)
        if door is None:
            raise exit_relay.Judged(403, REFUSAL_NO_VALUE)
        table = _state.table
        token = table.get(door.token_hash, entry.placeholder)
        if token is None:
            revoked = table.revoked(door.token_hash, entry.placeholder)
            if revoked:
                raise exit_relay.Judged(403, revoked)
            token = await table.wait_for(door.token_hash, entry.placeholder, _PUSH_WAIT_S)
        if token is None:
            raise exit_relay.Judged(403, table.revoked(door.token_hash, entry.placeholder) or REFUSAL_NO_VALUE)
        return str(token.value)

    async def addresses(self, sid: str, host: str, port: int) -> list[str] | None:
        if not _state.compute:
            return [host]
        from .._vendored import outbound_url
        door = _door_of(sid)
        carves = door.carves if door is not None else frozenset()
        infos = await asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM)
        seen: list[str] = []
        for info in infos:
            address = str(info[4][0])
            if address not in seen and not outbound_url.sandbox_dial_refused(
                    address, carves=carves, onlink=_state.onlink):
                seen.append(address)
        return seen or None

    def client(self, sid: str, upstream: Any = None):
        """The forward's client: direct, or through the machine's own proxy
        (``upstream``), one of each per door. Neither reads this process's
        environment: the relay chose the way already."""
        import httpx

        from .._vendored import exit_relay
        door = _door_of(sid)
        if door is None:
            # The session's stop arrived mid-connection: nothing is sent on.
            raise exit_relay.Judged(403, REFUSAL_NO_VALUE)

        def made(**more: Any) -> Any:
            # The door's own trust, built at its start off the loop; read here
            # (once per client, never per request) only when it has none.
            verify = door.verify if door.verify is not None and _upstream_verify is True else _verify()
            return httpx.AsyncClient(
                verify=verify, follow_redirects=False, trust_env=False,
                timeout=httpx.Timeout(connect=_CONNECT_TIMEOUT_S, read=None, write=60.0, pool=10.0),
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=16), **more)

        if upstream is None:
            if door.client is None:
                door.client = made()
            return door.client
        held = door.proxied.get(upstream.url)
        if held is not None and held[0] == upstream.authorization:
            return held[1]
        if held is not None:
            # The proxy's credential changed (a sign-in brought another): the
            # old client goes, the next ones use the new one.
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(held[1].aclose())
        headers = {"Proxy-Authorization": upstream.authorization} if upstream.authorization else None
        client = made(proxy=httpx.Proxy(upstream.url, headers=headers))
        door.proxied[upstream.url] = (upstream.authorization, client)
        return client

    async def upstream(self, sid: str, host: str, port: int):
        """The machine's own outgoing proxy for this target, or None to dial
        it directly: none set, a target its bypass names, or a proxy the
        relay does not speak (the listener reports that one)."""
        from .._vendored import exit_relay
        url = _upstream_url()
        if not url:
            return None
        try:
            up = exit_relay.parse_upstream(url)
        except exit_relay.UpstreamRefused:
            return None
        if up is None or await _bypassed(host):
            return None
        return up

    async def upstream_admits(self, sid: str, host: str, port: int) -> bool:
        """Whether a target the machine's proxy will resolve may be asked of
        it. On a paired computer every target, as its sessions reach them.
        On a compute machine the sandbox's rule, best effort: an address, and
        every answer the resolver gives here within its bound, must pass it;
        a name that only means this machine never does; a name this machine
        cannot resolve goes to the proxy, which resolves it."""
        if not _state.compute:
            return True
        from .._vendored import outbound_url
        door = _door_of(sid)
        carves = door.carves if door is not None else frozenset()

        def passes(address: str) -> bool:
            return not outbound_url.sandbox_dial_refused(address, carves=carves, onlink=_state.onlink)
        try:
            ipaddress.ip_address(host.strip("[]"))
        except ValueError:
            pass
        else:
            return passes(host.strip("[]"))
        if outbound_url.names_this_machine(host):
            return False
        name = host.lower().rstrip(".")
        now = time.monotonic()
        if _state.unresolved.get(name, 0.0) > now:
            return True
        try:
            infos = await asyncio.wait_for(
                asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM), _LOOKUP_S)
        except (OSError, asyncio.TimeoutError, TimeoutError):
            _state.unresolved[name] = now + _UNRESOLVED_TTL_S
            return True
        return all(passes(str(info[4][0])) for info in infos)

    def forward_url(self, sid: str, host: str, port: int, path: str, addresses: list[str]):
        if not _state.compute:
            return (f"https://{host}:{port}{path}" if port != 443 else f"https://{host}{path}"), {}
        address = addresses[0]
        literal = f"[{address}]" if ":" in address else address
        return f"https://{literal}:{port}{path}", {"sni_hostname": host}

    async def leaf_context(self, host: str):
        # A cached leaf from memory; a new one, and the bundle's compare
        # before it, on the exit's own thread: this loop carries the link too.
        authority = _state.authority
        cached = authority.cached_context(host) if authority is not None else None
        if cached is not None:
            return cached
        return await asyncio.get_running_loop().run_in_executor(_make_pool(), _new_leaf, host)

    async def judge(self, sid: str, entry, method: str, host: str, target: str, about: dict,
                    body: bytes | None):
        """The platform's word on one request of a judged entry. Nothing
        but a verdict lets it go: a door with no tunnel or no token, an ask
        that fails or outlasts its time and an answer that is no verdict all
        refuse the request."""
        from .._vendored import exit_relay
        door = _door_of(sid)
        refused = exit_relay.Judgement(status=503, detail=REFUSAL_NO_WORD)
        if door is None or not door.platform_url or not door.cli_token:
            logger.warning("credential exit: %s: a request of %s could not be checked: this session's door "
                           "holds no way to ask the platform", sid[:8], entry.key)
            return refused
        ask: dict[str, Any] = {
            "placeholder": entry.placeholder, "method": method, "host": host, "target": target,
            "content_type": str(about.get("content_type") or ""),
            "content_encoding": str(about.get("content_encoding") or ""),
        }
        if body is not None:
            ask["body_b64"] = base64.b64encode(body).decode("ascii")
        try:
            word = await asyncio.wait_for(_ask(door, {"session_id": sid, "host": host, "request": ask}),
                                          _JUDGE_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001  # whatever the ask met, the request does not go unjudged
            logger.warning("credential exit: %s: the platform did not say whether a request of %s may go: %s",
                           sid[:8], entry.key, type(exc).__name__)
            return refused
        if word is None:
            logger.warning("credential exit: %s: the platform's answer about a request of %s is no verdict",
                           sid[:8], entry.key)
            return refused
        return word


__all__ = [
    "AUTHORITY_DIRNAME", "BUNDLE_NAME", "REFUSAL_NO_VALUE", "bundle_path", "heal_soon", "set_platform_egress", "upstream_on",
    "owns_socket", "port", "render_values", "start", "start_listener", "state", "stop", "stop_listener",
]

