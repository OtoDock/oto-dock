"""The listing job: one MCP server started for one agent the way an app
button starts it (``services/apps/headless_exec.open_manager``), so its tool
list is read and the labels the tools carry land in ``mcp_tool_labels``
(``core/layers/direct/mcp.py`` records them at every start), then closed.
The Outside people picks read those labels: a Read only pick gives the
functions the maker labelled read-only, so a server is listed when a
manager enables it on an agent, when a pick lands on a server never listed,
and again when the server is updated or reinstalled (a maker's relabel then
reaches the picks and the managers' notice). The manager is built as an
outside session is, not as a task, so a server a manifest keeps out of
tasks is listed all the same. Best effort and bounded: a failure logs one
line and the page shows the server as unlisted. The server runs where an
app button's does: on the compute machine that serves the platform's place.
With none connected, the listing runs at the next sign-in of one
(``retry_owed``). A server its manifest places on a person's or an admin's
machine is not listed here.

It also keeps what a tool's program answered when a session started it (its
handshake and its lists, ``mcp_tool_listings``), so the next session of the
same agent and person answers the engine from it and starts the program only
at the agent's first call (the stand-in, ``core/stdio_path_interceptor.py``,
and Direct LLM's own client). A saved answer is keyed by a fingerprint of the
tool's version, account, command line and environment, served only to the
agent and person it was saved for, and never a source of labels: the four
hints of each tool come from the labels above, which only the platform's own
reads write.
"""

from __future__ import annotations

import asyncio
import json
import logging

import config
from core.stdio_path_interceptor import STANDIN_ENV
from services.mcp import mcp_permissions, mcp_registry, outside_picks

__all__ = ["STANDIN_ENV"]

logger = logging.getLogger("otodock.mcp")

LIST_TIMEOUT_S = 30.0
_jobs: set[asyncio.Task] = set()
#: The listings refused because no compute machine was connected: they run
#: at the next sign-in (``retry_owed``). In memory, as the jobs are.
_owed: set[tuple[str, str]] = set()


def server_key_of(mcp_name: str) -> str:
    manifest = mcp_registry.get_manifest(mcp_name)
    return outside_picks.server_key(manifest) if manifest else mcp_name


def is_listed(mcp_name: str) -> bool:
    return bool(mcp_permissions.known_tools(server_key_of(mcp_name)))


async def list_tools(agent: str, mcp_name: str) -> bool:
    """Start the server for the agent, which records its labels, and close
    it. True when the server's functions are known afterwards."""
    from services.apps import headless_exec
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None or manifest.placement != "any":
        logger.info("tool listing: %s of agent %s is not listed here (%s)", mcp_name, agent,
                    "no manifest" if manifest is None else "it runs on a machine")
        return False
    key = outside_picks.server_key(manifest)
    try:
        entry = await asyncio.wait_for(
            headless_exec.open_manager(agent, {}, frozenset({key}), prefix="list", phone=True),
            timeout=LIST_TIMEOUT_S,
        )
    except Exception as e:  # noqa: BLE001  # a server that does not start is left unlisted, with one line
        from core import placement
        if isinstance(e, placement.NoComputeMachine):
            _owed.add((agent, mcp_name))
        logger.warning("tool listing: %s of agent %s did not start: %s", mcp_name, agent, e)
        return False
    try:
        listed = bool(mcp_permissions.known_tools(key))
    finally:
        await headless_exec.close_manager(entry)
    logger.info("tool listing: %s of agent %s %s", mcp_name, agent,
                "listed" if listed else "answered with no tool")
    return listed


def retry_owed() -> int:
    """A compute machine signed in: the listings its absence refused run
    now, in the background. Answers how many."""
    owed = sorted(_owed)
    _owed.clear()
    for agent, mcp_name in owed:
        schedule(agent, mcp_name)
    return len(owed)


def listing_agent_for(mcp_name: str) -> str:
    """One agent that has the server enabled, "" when none does: first an
    agent whose Outside people picks name it (the listing then runs on the
    credentials the picks will use), then any that enabled it. The labels
    are per server key, so one listing serves every agent's picks."""
    from storage.agents import outside_store
    from storage.mcp import mcp_store
    picked = outside_store.agents_with_pick(mcp_name)
    enabled = mcp_store.get_mcp_agents(mcp_name)
    for agent in picked + [a for a in enabled if a not in picked]:
        if any(m.name == mcp_name for m in mcp_registry.get_agent_mcps_all_placements(agent)):
            return agent
    return ""


BUNDLED_VERSIONS_KEY = "outside_listed_bundled_versions"


def _relist_bundled() -> list[str]:
    """The bundled MCPs (``mcps/custom``, shipped with the platform) that
    some agent's picks name and whose manifest version moved since they
    were last listed: a platform upgrade changes them with no install."""
    from services.mcp.mcp_manifest_blocks import ships_with_platform
    from storage import database as task_store
    from storage.agents import outside_store
    try:
        seen = json.loads(task_store.get_platform_setting(BUNDLED_VERSIONS_KEY) or "{}")
    except ValueError:
        seen = {}
    moved: list[str] = []
    for name in outside_store.picked_mcp_names():
        manifest = mcp_registry.get_manifest(name)
        if manifest is None or not ships_with_platform(manifest.mcp_dir):
            continue
        if seen.get(name) != manifest.version:
            moved.append(name)
            seen[name] = manifest.version
    if moved:
        task_store.set_platform_setting(BUNDLED_VERSIONS_KEY, json.dumps(seen, sort_keys=True))
    return moved


async def relist_bundled_after_upgrade() -> str | None:
    """At boot: list again each bundled tool a pick names whose version
    changed with the platform (``startup._boot_continue``)."""
    moved = await asyncio.to_thread(_relist_bundled)
    for name in moved:
        schedule_after_install(name)
    return f"{len(moved)} bundled tool(s) listed again" if moved else None


async def after_install(mcp_name: str) -> None:
    """An update or a reinstall of ``mcp_name`` ended: list it again in the
    background through one agent that has it, so a changed label reaches
    the picks. A skill package has no server, an unknown name nothing."""
    from services.mcp.mcp_agent_skills import is_skill_package
    manifest = mcp_registry.get_manifest(mcp_name)
    if manifest is None or is_skill_package(manifest):
        return
    await asyncio.to_thread(forget, server=outside_picks.server_key(manifest))
    agent = await asyncio.to_thread(listing_agent_for, mcp_name)
    if agent:
        schedule(agent, mcp_name)


def _spawn(coro) -> None:
    """On the running loop (a route's, a job's), in the background; a
    caller with no loop lists nothing."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        return
    task = loop.create_task(coro)
    _jobs.add(task)
    task.add_done_callback(_jobs.discard)


def schedule(agent: str, mcp_name: str) -> None:
    """The listing in the background."""
    _spawn(list_tools(agent, mcp_name))


def schedule_after_install(mcp_name: str) -> None:
    """``after_install`` in the background: the update's job ends at once."""
    _spawn(after_install(mcp_name))


# --- What a tool's program answered, for the next session -------------------

#: The saved answers kept per agent, person and tool (the most recently used).
KEEP_PER_TOOL = 8
#: Saved answers nobody used for this long go.
UNUSED_DAYS = 30
MAX_LISTING_BYTES = 1024 * 1024
#: A tool gathering this many fingerprints in a day for one agent and person
#: (each engine and machine kind holds its own, by design) fills its keep:
#: from there the oldest go, and one log line says so.
CHURN_FINGERPRINTS = KEEP_PER_TOOL
#: The person an outside person's saved answers are kept under: one per
#: channel and caller (``outside_person``), so ``forget`` follows one of them.
OUTSIDE_PREFIX = "outside:"
_TOUCH_EVERY_S = 24 * 3600
_LIST_METHODS = ("tools/list", "prompts/list", "resources/list", "resources/templates/list")


def stamp_of(manifest) -> str:
    """The version a saved answer is good for: the tool's own, and for a tool
    that ships with the platform the platform's too (it changes with no
    version of its own)."""
    from services.mcp.mcp_manifest_blocks import ships_with_platform
    stamp = str(getattr(manifest, "version", "") or "")
    mcp_dir = getattr(manifest, "mcp_dir", None)
    if mcp_dir is not None and ships_with_platform(mcp_dir):
        stamp += "+" + (config.PINNED_OTODOCK_VERSION or "")
    return stamp


def starts_late(manifest, *, eager: bool) -> bool:
    """Whether a session's stdio entry of ``manifest`` gets a stand-in: every
    one but an eager build's (the listing job reads the program itself) and a
    tool that says it starts with the chat. A call's tools get one too, warm
    (``marker``): answered at once, their programs started beside the engine."""
    from services.mcp.mcp_manifest_blocks import STARTS_WITH_CHAT
    from services.mcp.mcp_manifest_types import is_stdio_transport
    server = getattr(manifest, "server", None)
    if eager or server is None:
        return False
    return (is_stdio_transport(getattr(server, "transport", "stdio"))
            and getattr(server, "starts", "") != STARTS_WITH_CHAT)


def marker(manifest, *, account: str, fetch: bool, warm: bool = False) -> str:
    """The stand-in's marker (``OTO_TOOL_STANDIN``): the version stamp, a hash
    of the account the build resolved, whether the tool's start needs its
    fetch to answer (it has secrets), its name for a sentence, and for a
    call's tool ``warm``: its program starts with the session, beside the
    engine, and its session counted it at its start."""
    import hashlib
    fields = {
        "v": 1, "stamp": stamp_of(manifest),
        "account": hashlib.sha256(account.encode()).hexdigest()[:16] if account else "",
        "fetch": bool(fetch),
        "label": str(getattr(manifest, "label", "") or getattr(manifest, "name", "") or ""),
    }
    if warm:
        fields["warm"] = True
    return json.dumps(fields, separators=(",", ":"), sort_keys=True)


def is_warm(marker_text: str) -> bool:
    """Whether a stand-in's marker is a call's (``marker``)."""
    try:
        parsed = json.loads(marker_text)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get("warm") is True


def outside_person(channel: str, external_id: str) -> str:
    """The person key of one outside person: their channel and caller id."""
    return f"{OUTSIDE_PREFIX}{channel}:{external_id}"


def scope_of(ctx) -> tuple[str, str]:
    """``(agent, person)`` a session's saved answers belong to: its agent and
    its username, one key per outside person (``outside_person``), "" for the
    agent's own runs."""
    from core.session.external_identity import is_external_ctx
    agent = str(getattr(ctx, "agent", "") or "")
    if is_external_ctx(ctx):
        return agent, outside_person(str(getattr(ctx, "external_channel", "") or ""),
                                     str(getattr(ctx, "external_id", "") or ""))
    return agent, str(getattr(ctx, "username", "") or "")


def _trusted_hints(server: str, tools: list) -> list:
    """Each tool's four hints from the platform's labels, dropped where none
    are known; the rest of its annotations (its title) stays."""
    out = []
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool = dict(tool)
        annotations = {k: v for k, v in (tool.get("annotations") or {}).items()
                       if k not in mcp_permissions.HINT_KEYS} if isinstance(tool.get("annotations"), dict) else {}
        hints = mcp_permissions.tool_hints(server, str(tool.get("name") or ""))
        if hints:
            annotations.update({k: v for k, v in hints.items() if k in mcp_permissions.HINT_KEYS})
        if annotations:
            tool["annotations"] = annotations
        else:
            tool.pop("annotations", None)
        out.append(tool)
    return out


def saved(scope: tuple[str, str], server: str, fingerprint: str) -> dict | None:
    """The answers saved for this scope, tool and fingerprint, with the
    trusted labels in place of the program's; None when there are none."""
    from storage.mcp import mcp_store
    agent, person = scope
    row = mcp_store.get_tool_listing(agent, person, server, fingerprint)
    if row is None:
        return None
    try:
        listing = json.loads(row["listing"])
    except ValueError:
        return None
    if not isinstance(listing, dict):
        return None
    lists = listing.get("lists") or {}
    if isinstance(lists.get("tools/list"), list):
        lists["tools/list"] = _trusted_hints(server, lists["tools/list"])
    if _older_than(row.get("used_at") or "", _TOUCH_EVERY_S):
        mcp_store.touch_tool_listing(agent, person, server, fingerprint)
    return listing


def _older_than(stamp: str, seconds: float) -> bool:
    from datetime import datetime, timezone
    try:
        then = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return (datetime.now(timezone.utc) - then).total_seconds() > seconds


def _clean(listing) -> dict | None:
    """A saved answer's shape, or None: a handshake object and lists of
    objects, each tool named by a string; the hints a program sent are not
    kept (the labels are the platform's own)."""
    if not isinstance(listing, dict) or not isinstance(listing.get("initialize"), dict):
        return None
    raw_lists = listing.get("lists")
    lists_in: dict = raw_lists if isinstance(raw_lists, dict) else {}
    lists: dict[str, list] = {}
    for method in _LIST_METHODS:
        items = lists_in.get(method)
        if items is None:
            continue
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            return None
        lists[method] = items
    init = listing["initialize"]
    caps = init.get("capabilities") if isinstance(init.get("capabilities"), dict) else {}
    tools = lists.get("tools/list") or []
    if "tools" in caps and not tools:
        # A list with no tool is never called, so never started again: such
        # a program starts with the chat (a service down, a key not yet set).
        return None
    if not all(isinstance(t.get("name"), str) and t.get("name") for t in tools):
        return None
    for tool in tools:
        if isinstance(tool.get("annotations"), dict):
            tool["annotations"] = {k: v for k, v in tool["annotations"].items()
                                   if k not in mcp_permissions.HINT_KEYS}
    kept_init = {k: init[k] for k in ("protocolVersion", "capabilities", "serverInfo", "instructions")
                 if k in init}
    return {"initialize": kept_init, "lists": lists}


def keep(scope: tuple[str, str], server: str, fingerprint: str, listing) -> bool:
    """Keep what a tool's program answered, for the next session of this
    scope. False when the answer is malformed or too large. A tool whose
    functions the platform has never labelled, and that the listing job can
    reach, is listed once by it, so its labels come from the platform's own
    read."""
    from storage.mcp import mcp_store
    agent, person = scope
    cleaned = _clean(listing)
    if cleaned is None or not agent or not server or not fingerprint:
        return False
    text = json.dumps(cleaned, separators=(",", ":"))
    if len(text) > MAX_LISTING_BYTES:
        logger.info("tool listing: %s of agent %s answered %d bytes, not kept", server, agent, len(text))
        return False
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=UNUSED_DAYS)).isoformat()
    recent = mcp_store.put_tool_listing(agent, person, server, fingerprint, text,
                                        keep=KEEP_PER_TOOL, unused_before=cutoff)
    if recent == CHURN_FINGERPRINTS:
        logger.warning("tool listing: %s of agent %s gathered %d fingerprints for one person in a day, "
                       "its keep; the oldest go from here. If this repeats daily, a value of its "
                       "environment changes per session", server, agent, recent)
    if not mcp_permissions.known_tools(server):
        manifest = mcp_permissions.manifest_for_server(server)
        if manifest is not None and manifest.placement == "any":
            schedule(agent, manifest.name)
    return True


#: The person a Docker tool's container record is kept under: the platform's
#: own read of one shared container, served to every agent and person that
#: has the tool. ``scope_of`` never answers it, so no session's report can
#: write it.
PLATFORM_PERSON = "platform:container"


def container_fingerprint(manifest, account: str = "") -> str:
    """What a Docker tool's record is good for: its version stamp, the
    ``.env`` its container is started with (a saved setting, a tool filter),
    its compose files and, for a tool with a sign-in, the account the record
    was read with, hashed as ``marker`` hashes a program's (its record is
    that account's alone). A change of any gives a new one."""
    import hashlib

    from services.mcp import docker_manager
    h = hashlib.sha256(stamp_of(manifest).encode())
    if account:
        h.update(b"account:" + hashlib.sha256(account.encode()).hexdigest()[:16].encode())
    mcp_dir = getattr(manifest, "mcp_dir", None)
    if mcp_dir is not None:
        try:
            h.update((mcp_dir / ".env").read_bytes())
        except OSError:
            h.update(b"-")
    h.update((docker_manager._compose_config_hash(manifest) or "").encode())
    return h.hexdigest()[:32]


def saved_platform(server: str, fingerprint: str) -> dict | None:
    """A Docker tool's record for this fingerprint, with the trusted labels
    in place of the server's; None when there is none."""
    from storage.mcp import mcp_store
    row = mcp_store.find_tool_listing(PLATFORM_PERSON, server, fingerprint)
    if row is None:
        return None
    return saved((str(row["agent_name"]), PLATFORM_PERSON), server, fingerprint)


def keep_platform(server: str, fingerprint: str, listing, agents) -> bool:
    """Keep a Docker tool's record under each agent that has it (the row's
    agent is a key of the table; a record outlives any one agent's removal).
    False when the answer is malformed or too large, or no agent has it."""
    kept = False
    for agent in agents:
        kept = keep((agent, PLATFORM_PERSON), server, fingerprint, listing) or kept
    return kept


def forget(*, server: str | None = None, person: str | None = None, agent: str | None = None) -> int:
    """Drop saved answers: a tool's (its update or reinstall), a person's
    (their removal), or one outside person's on one agent (``agent``); an
    agent's go with the agent's row."""
    from storage.mcp import mcp_store
    return mcp_store.delete_tool_listings(server_name=server, person=person, agent_name=agent)


def sweep_unused(*, dry_run: bool = False) -> int:
    """The daily retention pass: every saved answer nobody used for
    ``UNUSED_DAYS`` (a person who left an agent, a tool taken off it); how
    many (counted only on a dry run)."""
    from datetime import datetime, timedelta, timezone

    from storage.mcp import mcp_store
    cutoff = (datetime.now(timezone.utc) - timedelta(days=UNUSED_DAYS)).isoformat()
    return mcp_store.delete_unused_tool_listings(cutoff, dry_run=dry_run)


# -- a terminal's tools before its first message -------------------------
#
# A Claude Code terminal sends its first message the moment it is submitted,
# without the tool servers still connecting (it adds them later in the
# turn). Each stand-in reports when the engine has its tool list (``listed``
# on ``POST /v1/hooks/tool-start``, the stand-in's own fetch token), and the
# terminal holds a waiting first message until every stand-in of the session
# has reported or its bound passes (``core/session/interactive_session``).
# In memory: a restart starts the terminals again.

_expected: dict[str, set[str]] = {}


def standin_servers_of(mcp_servers: dict) -> list[str]:
    """The servers of a Claude Code tool config (``mcpServers``) that run
    behind a stand-in (their env carries its marker)."""
    out: list[str] = []
    for name, entry in (mcp_servers or {}).items():
        env = entry.get("env") if isinstance(entry, dict) else None
        if isinstance(env, dict) and STANDIN_ENV in env:
            out.append(str(name))
    return out


def expect_listed(session_id: str, servers) -> None:
    """The stand-ins a terminal session's first message waits for (an
    empty set waits for nothing). Called when the session's config is built,
    before the engine starts."""
    names = {str(s) for s in servers or ()}
    if names:
        _expected[session_id] = names
    else:
        _expected.pop(session_id, None)


def note_listed(session_id: str, server: str) -> None:
    """The engine has ``server``'s tool list (the stand-in said so)."""
    pending = _expected.get(session_id)
    if pending is not None:
        pending.discard(server)


def pending_listed(session_id: str) -> frozenset[str]:
    """The stand-ins of the session the engine has no list from yet."""
    return frozenset(_expected.get(session_id) or ())


def drop_listed(session_id: str) -> None:
    _expected.pop(session_id, None)
