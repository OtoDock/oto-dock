"""The idle sweep: a Docker tool's container put to sleep after a quiet spell
(``config.OTODOCK_TOOLS_IDLE_MINUTES``, 0 for never), one item of the 60 s
sweep (``startup_sweeps``). Its clock is the door's (``state``): a request in
flight or a session bound to the tool is use, the end of the last request
starts the spell, and a start of the platform counts as use.

A tool is left running while it stays warm (``lifecycle.stays_warm``), an
update of it waits, a compose run holds its lock, or a session built before
this start of the platform dials it directly (its config names the
container: a session taken back across the upgrade that brought the door).
A tool whose manifest declares ``server.busy_endpoint`` is asked before it
sleeps, and one that says it is working stays up, for at most its declared
``tool_timeout_sec`` (``HOLD_DEFAULT_S`` when none) from the first yes of its
quiet spell: a stuck job never pins it for ever.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import config
from services.containers import door, lifecycle, state

logger = logging.getLogger("otodock.containers")

# How long a tool that declares no call limit may keep itself awake, and how
# long the sweep waits for its answer.
HOLD_DEFAULT_S = 30 * 60
ASK_TIMEOUT_S = 2.0
# The longest answer read: more is a no, never buffered.
ASK_MAX_BYTES = 1024

_reconciled = False
# Per session built before this start: the Docker tools its config dials
# directly (read once, at the first sweep).
_direct: dict[str, frozenset[str]] | None = None


def _enabled_tools() -> list:
    from services.mcp import mcp_manifest_types as _mt, mcp_registry
    from storage.mcp import mcp_store
    states = mcp_store.get_all_mcp_states()
    return [m for name, m in mcp_registry.get_all_manifests().items()
            if _mt.is_container(m.server) and states.get(name, False) and lifecycle.managed(m)]


def _reconcile(manifests) -> None:
    """At the first sweep after a start: which containers do not run (the
    platform does not remember what it put to sleep before)."""
    from services.mcp import docker_manager
    for m in manifests:
        if docker_manager.get_container_status(m) not in docker_manager.PRESENT:
            state.of(m.name).asleep = True


def _direct_dials(manifests) -> dict[str, frozenset[str]]:
    """The Docker tools each session taken back at this start dials
    directly, by its config file (a session built before the door). The
    sessions that outlive a restart are the provisioned ones, each with a
    gateway descriptor named by its id; a session built after the start
    names the gateway's route and counts for nothing here."""
    from urllib.parse import urlsplit

    from core.credentials import mcp_gateway
    from core.session import session_state
    from services.mcp import mcp_session_config
    ports = {int(m.server.port): m.name for m in manifests if getattr(m.server, "port", None)}
    out: dict[str, frozenset[str]] = {}
    for desc in mcp_gateway.descriptor_dir().glob("*.json"):
        sid = desc.stem
        if session_state.get_session_security(sid) is None:
            continue
        folder = mcp_session_config.session_build_dir(sid)
        names: set[str] = set()
        for path in list(folder.glob("*.json")) if folder.is_dir() else []:
            try:
                doc = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            servers = doc.get("mcpServers", doc) if isinstance(doc, dict) else {}
            for entry in (servers.values() if isinstance(servers, dict) else ()):
                url = str(entry.get("url") or "") if isinstance(entry, dict) else ""
                if not url or mcp_gateway.gateway_key_of(url) is not None:
                    continue
                try:
                    port = urlsplit(url).port
                except ValueError:
                    continue
                if port in ports:
                    names.add(ports[port])
        if names:
            out[sid] = frozenset(names)
    return out


def held_directly(name: str) -> bool:
    """A live session built before this start dials the tool directly."""
    from core.session import session_state
    return any(name in names and session_state.get_session_security(sid) is not None
               for sid, names in (_direct or {}).items())


async def _still_working(manifest) -> bool:
    """The tool's own answer at its ``busy_endpoint``, on its own port, by
    the address the door's probe dials, never through the door (a question
    is no use). The whole ask is bounded in time and size, since the sweep
    asks the tools one by one: no answer, an error, a slow or long answer or
    anything but ``{"busy": true}`` is no."""
    from urllib.parse import urlsplit

    from core.credentials import mcp_gateway
    from services.mcp import mcp_registry

    async def ask(url: str) -> bool:
        async with mcp_gateway.client(True).stream("GET", url, timeout=ASK_TIMEOUT_S) as answer:
            if answer.status_code != 200:
                return False
            body = b""
            async for chunk in answer.aiter_bytes():
                body += chunk
                if len(body) > ASK_MAX_BYTES:
                    return False
        said = json.loads(body)
        return isinstance(said, dict) and said.get("busy") is True

    try:
        entry = mcp_registry.resolve_server_config(manifest, "", mcp_config_format="json")
        base = urlsplit(str(entry.get("url") or ""))
        if not base.scheme or not base.netloc:
            return False
        return await asyncio.wait_for(
            ask(f"{base.scheme}://{base.netloc}{manifest.server.busy_endpoint}"), ASK_TIMEOUT_S)
    except Exception:  # noqa: BLE001  # any failure of the ask is a no: the tool sleeps as before
        return False


def hold_bound_s(manifest) -> int:
    """How long the tool may keep itself awake: its declared call limit."""
    return int(getattr(manifest.server, "tool_timeout_sec", 0) or 0) or HOLD_DEFAULT_S


def _keeps_awake(manifest, st, clock: float) -> bool:
    """A yes within the bound keeps it awake; the first one is logged, and
    one past the bound is refused with a warning."""
    if not st.held_since:
        st.held_since = clock
        logger.info("Docker MCP %s: it is working, so it stays awake for up to %d minutes",
                    manifest.name, hold_bound_s(manifest) // 60)
    if clock - st.held_since < hold_bound_s(manifest):
        return True
    logger.warning("Docker MCP %s: still working after %d minutes, put to sleep anyway",
                   manifest.name, hold_bound_s(manifest) // 60)
    return False


def _sleep_if_free(manifest) -> bool:
    """Put it to sleep unless a compose run holds its lock."""
    st = state.of(manifest.name)
    if not st.lock.acquire(blocking=False):
        return False
    try:
        return lifecycle.sleep_container(manifest)
    finally:
        st.lock.release()


async def sweep_once(now: float | None = None) -> list[str]:
    """One pass: the tools put to sleep."""
    global _reconciled, _direct
    # A session gone without its close (its context dropped) lets its tools'
    # server sessions go, whatever the spell: they would hold a browser's
    # pages open for good.
    door.end_orphans()
    minutes = config.OTODOCK_TOOLS_IDLE_MINUTES
    if minutes <= 0:
        return []
    from services.mcp import mcp_updater
    from storage.mcp import mcp_store
    manifests = await asyncio.to_thread(_enabled_tools)
    if not _reconciled:
        await asyncio.to_thread(_reconcile, manifests)
        _direct = await asyncio.to_thread(_direct_dials, manifests)
        _reconciled = True
    keep = await asyncio.to_thread(mcp_store.get_keep_running)
    slept: list[str] = []
    for manifest in manifests:
        name = manifest.name
        st = state.of(name)
        clock = time.monotonic() if now is None else now
        if clock - st.last_use < minutes * 60:
            st.held_since = 0.0   # any use ends a hold
        if (st.asleep or st.waking or lifecycle.stays_warm(manifest, keep)
                or clock - st.last_use < minutes * 60 or mcp_updater.update_running(name)
                or held_directly(name)):
            continue
        if getattr(manifest.server, "busy_endpoint", ""):
            # Asked before the window below, which must hold no await; a
            # use that began while it was asked keeps it as any use does.
            last = st.last_use
            working = await _still_working(manifest)
            if not working:
                st.held_since = 0.0
            if st.last_use != last or (working and _keeps_awake(manifest, st, clock)):
                continue
        # Checked and marked with no await between: a request that comes
        # after this waits for the stop and wakes the tool.
        if state.in_use(name):
            continue
        st.asleep = True
        if await asyncio.to_thread(_sleep_if_free, manifest):
            door.forget_tool(name)
            st.held_since = 0.0
            slept.append(name)
        else:
            st.asleep = False
    return slept


def reset_for_tests() -> None:
    global _reconciled, _direct
    _reconciled = False
    _direct = None
