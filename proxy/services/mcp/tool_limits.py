"""The tool limits of an agent: which functions of an enabled MCP its
sessions may use, by who runs the session, read once at a session's build
from the rows the Outside people picks live in (``agent_outside_tools``,
``storage/agents/outside_store.py``) and resolved against the tools' labels
as they stand then.

Three words say which sessions a row governs (``governs_of``): ``outside``
for an outside person's session (a pick: it opens a tool to them, and
``outside_picks`` reads those rows), ``runs`` for a session with no person
behind it (the agent's own tasks, its line, the wrap-up, an agent-scope
meeting or app run), ``chats`` for every session with a person behind it
(their chats in every mode, their tasks and workers, the meetings they
convene, the app buttons they press). A limit is ``read_only`` (the
functions the labels marked read-only when the limit was saved, kept as
saved: a function the maker adds later stays off until a manager saves the
limit again) or ``custom`` (a list of names). A row with ``read_write`` is
no limit and is never written for a limit.

One decision reads a limit: ``api/hooks/limits.limit_refusal`` in the
permission authority, before every mode branch. The grants a build resolves
here ride the session's security context (``SecurityContext.tool_limits``),
where the hook, the gateway and an app button's run judge each call by
function name, and the engines are told not to offer the functions left
off. A server whose limit grants nothing is left out of the session by the
one rule (``mcp_session_filter.session_exclusion_reason``, with the reasons
below).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from services.mcp.outside_picks import (
    CUSTOM,
    READ_ONLY,
    read_only_functions,
    server_key,
)
from storage.agents.outside_store import CHATS, GOVERNS, OUTSIDE, RUNS

__all__ = ["CHATS", "GOVERNS", "OUTSIDE", "RUNS"]

#: Why a limited server is left out of a session.
LIMIT_UNLISTED_REASON = "Limited to the tools that read, and its tools are not listed yet"
LIMIT_EMPTY_REASON = "Limited for this agent and none of its tools may be used"

#: The limit every task-kind session carries on the agent's self-config
#: tool: a run may reload its own session or switch its account, and
#: nothing else of that tool, whatever the tool's own listing says.
AGENT_CONFIG_MCP = "agent-config-mcp"
TASK_SESSION_LIMIT: tuple[tuple[str, tuple[str, ...]], ...] = (
    (AGENT_CONFIG_MCP, ("reload_session", "switch_account")),
)

#: Where a manager sets a limit, in the words a sentence gives.
CARD_PLACE = "Agent Settings, Configuration, What the agent may use"


@dataclass(frozen=True)
class Limit:
    access: str
    functions: tuple[str, ...] = ()
    granted: tuple[str, ...] = ()


def governs_of(*, external: bool, person: str | None) -> str:
    """The word a session takes: an outside person's, else by whether a
    person's identity is behind it (the person its credentials resolved
    for), never by which builder made it."""
    if external:
        return OUTSIDE
    return CHATS if person else RUNS


def load(agent_name: str, governs: str) -> dict[str, Limit]:
    """The agent's limits of one column, by manifest name. Synchronous (the
    DB executor). Never the outside column: those rows are picks
    (``outside_picks.load``)."""
    if governs == OUTSIDE:
        return {}
    from storage.agents import outside_store
    rows = outside_store.get_tool_picks(agent_name, governs=governs)
    return {name: Limit(row["access"], tuple(row["functions"]), tuple(row["granted"]))
            for name, row in rows.items() if row["access"] in (READ_ONLY, CUSTOM)}


def names_of(limit: Limit, key: str) -> tuple[str, ...]:
    """The functions a limit grants on server ``key`` now: the stored names
    of a custom limit; of a Read only one the set saved with it, less any
    function the maker no longer labels read-only. Sorted, each once."""
    if limit.access == CUSTOM:
        return tuple(sorted(set(limit.functions)))
    return tuple(sorted(set(limit.granted) & set(read_only_functions(key))))


def exclusion_reason(limit: Limit, key: str) -> str | None:
    """Why a server with this limit is left out of a session, None when it
    attaches: a Read only limit on a server never listed, or any limit that
    grants no function."""
    from services.mcp import mcp_permissions
    if limit.access == READ_ONLY and not mcp_permissions.known_tools(key):
        return LIMIT_UNLISTED_REASON
    if not names_of(limit, key):
        return LIMIT_EMPTY_REASON
    return None


def grants(limits: Mapping[str, Limit], attached: Iterable, *,
           forced: Iterable[str] = ()) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The pairs a session's context carries: ``(server key, names)`` for
    each attached manifest that has a limit, resolved at the build. A
    server without a limit has no pair; the names are never every function.
    ``forced``: the names of the servers the session forces in (the meetings
    tool in a meeting, the call tools on a call), the channel's own and
    never limited, whatever a row says."""
    skip = set(forced)
    out: list[tuple[str, tuple[str, ...]]] = []
    for manifest in attached:
        limit = limits.get(manifest.name)
        if limit is None or manifest.name in skip:
            continue
        out.append((server_key(manifest), names_of(limit, server_key(manifest))))
    return tuple(out)


def merged(*groups) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """One pair per server key out of several groups of pairs: the names
    every group's pair for that key allows (a manager's row on the
    self-config tool and a run's own pair give their intersection), in the
    order the keys were first met. Every builder that carries more than one
    source of limits combines them here, so each reader sees one pair."""
    out: dict[str, tuple[str, ...]] = {}
    for pairs in groups:
        for key, names in pairs or ():
            out[key] = _both(out[key], names) if key in out else tuple(names)
    return tuple(out.items())


def _both(first: tuple[str, ...], names) -> tuple[str, ...]:
    """The names of ``first`` that ``names`` allows too, in ``first``'s order."""
    keep = set(names)
    return tuple(n for n in first if n in keep)


def limited(pairs, key: str) -> tuple[str, ...] | None:
    """The names a session may call on server ``key``, None when the server
    has no limit in ``pairs``. Several pairs for one key (a caller that did
    not combine them) give what every one of them allows: a second pair
    never widens the first."""
    found: tuple[str, ...] | None = None
    for row_key, names in pairs or ():
        if row_key == key:
            found = tuple(names) if found is None else _both(found, names)
    return found


def allows(pairs, key: str, function: str) -> bool:
    """Whether a session may call ``function`` of server ``key``: a server
    with no pair keeps every function."""
    names = limited(pairs, key)
    return names is None or function in names


def refusal(pairs, key: str, function: str) -> str:
    """The sentence a refused call answers with, "" when the call may go on.
    The tool's label by its server key, its name when none is registered."""
    if allows(pairs, key, function):
        return ""
    from services.mcp import mcp_registry
    label = key
    for manifest in mcp_registry.get_all_manifests().values():
        if server_key(manifest) == key:
            label = manifest.label or manifest.name
            break
    return (f"{label} is limited for this agent: it may not use {function}. "
            f"A manager can change this in {CARD_PLACE}.")


def route_refusal(pairs, tools) -> str:
    """The sentence a first-party tool's route answers with when the
    session's limits leave off every tool that calls it
    (``auth/external_endpoints.ROUTE_TOOLS``), "" when one of them may go
    on: a route read before a tool's own call, or whose body names the
    action, serves several tools the path does not tell apart, so one
    allowed tool keeps it open. ``tools`` are ``(server key, function)``
    pairs; the sentence is the first refused tool's, the one the hook gives
    its call."""
    if not pairs:
        return ""
    first = ""
    for key, function in tools:
        sentence = refusal(pairs, key, function)
        if not sentence:
            return ""
        first = first or sentence
    return first


def engine_pairs(ctx) -> tuple:
    """The pairs an engine is told not to offer the rest of: an outside
    session's grants (every attached server, None for a whole one) and the
    session's limits. One rule for Claude's argv, Codex's ``enabled_tools``
    on the platform and in a machine's payload, and the Direct tool list."""
    from core.session.external_identity import is_external_ctx
    outside = tuple(getattr(ctx, "outside_grants", ()) or ()) if is_external_ctx(ctx) else ()
    return outside + tuple(getattr(ctx, "tool_limits", ()) or ())


def prompt_section(pairs, attached: Iterable[str] | None = None) -> str:
    """The prompt's "Tools you may use in part": one line per limited
    server naming what the agent may use, and what to say when asked for
    more. "" with no limit. ``attached``: the server keys the session has,
    when the caller knows them (a limit on a server left out is not named)."""
    if attached is not None:
        keys = set(attached)
        pairs = [(key, names) for key, names in pairs or () if key in keys]
    if not pairs:
        return ""
    from services.mcp import mcp_registry
    labels = {server_key(m): (m.label or m.name) for m in mcp_registry.get_all_manifests().values()}
    lines = [f"- {labels.get(key, key)} (`{key}`): you may use only {', '.join(names) or 'none of its tools'}."
             for key, names in pairs]
    return ("# Tools you may use in part\n\n" + "\n".join(lines) + "\n\n"
            "A tool left off is refused when called. If a person asks for it, say that a manager can "
            f"change this under {CARD_PLACE}.")
