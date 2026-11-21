"""Which MCPs a session of a given kind may have: the session contexts, the
external-caller denial and the audience checks a manifest declares.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from services.mcp import tool_envs
from services.mcp.mcp_manifest_store import get_agent_mcps_all_placements
from services.mcp.mcp_manifest_types import McpManifest
from services.mcp.outside_picks import APPS_MCP, NOT_PICKED_REASON, OutsidePicks

# ---------------------------------------------------------------------------
# External routes (phone callers who are not platform users)
# ---------------------------------------------------------------------------

#: Exclusion context matched against a manifest's ``exclude_from`` for every
#: EXTERNAL session (a phone call in the per-caller or shared identity mode —
#: anyone who is not a platform user). Community manifests may opt out of
#: external sessions with ``"exclude_from": ["external"]``.
EXTERNAL_CONTEXT = "external"

#: Exclusion context of a session that is not the token-named person's own
#: chat: a Shared-only agent's chat (the session runs as the agent), a task
#: chat reopened in the dashboard, a delegated worker's chat, a chat another
#: person's sign-in drives, an unattended wake, a build with no person. A
#: manifest lists it to load only where a person drives their own chat;
#: the chat builder passes it and an agent-scope mount implies it.
SHARED_CHAT_CONTEXT = "shared_chat"

#: MCPs that never attach to an external session regardless of what any
#: manifest declares or a manager picks: they manage the platform, schedule
#: deferred execution that would later run with the agent's full rights, or
#: expose infrastructure inventory; the memory tool, since the person's
#: memory is read into the prompt and written after the conversation. A
#: manifest can add itself to the exclusion (above) but nothing can remove a
#: name listed here.
EXTERNAL_DENIED_MCPS = frozenset({
    "delegation-mcp", "schedules-mcp", "triggers-mcp", "notifications-mcp",
    "meetings-mcp", "agent-config-mcp", "agent-creator-mcp", "mcps-mcp",
    "ssh-hosts", "phone-mcp", "memory-mcp", "conversations-mcp",
})

#: Exclusion reasons that describe the EXTERNAL rules themselves. The
#: prompt's "Unavailable Tools" list omits them on external sessions — a
#: caller-facing prompt must not enumerate the platform's management surface,
#: nor the tools nobody picked for outside people.
EXTERNAL_DENIED_REASON = "Not available on external routes"
EXTERNAL_CONTEXT_REASON = f"Excluded in {EXTERNAL_CONTEXT} mode"
EXTERNAL_REASONS = frozenset({EXTERNAL_DENIED_REASON, EXTERNAL_CONTEXT_REASON, NOT_PICKED_REASON})


#: The platform's conversations tool: a person's reads of the agent's
#: conversations, and the wrap-up's record of one. Never an outside session's.
CONVERSATIONS_MCP = "conversations-mcp"

#: A machine's screen and keyboard, and a signed-in browser: a shell and the
#: web by another name, off wherever the shell and the web are off.
MACHINE_CONTROL_MCPS = frozenset({"computer-control", "browser-control"})
#: First-party tools that fetch an address or search the web for the model:
#: the web by another name, which a wrap-up never has.
WEB_REACHING_MCPS = frozenset({"transcribe-mcp", "image-search-mcp"})


@dataclass(frozen=True)
class KindRule:
    """A session kind's own hard set and the platform tools it rides with:
    the wrap-up after a conversation and the agent's own phone line. The
    one rule reads it beside the outside people's; None for every other
    session."""

    denied: frozenset[str]
    reason: str
    admitted: frozenset[str] = frozenset()
    #: A session anyone may dial (the agent's own line): its prompt carries
    #: none of the agent's own records, no Notebook and no memory, it runs
    #: from a scratch engine home, and the agent's memory is shut to it.
    open_line: bool = False


#: The wrap-up after a conversation: the agent's business tools, never the
#: management ones an outside session is refused (the memory tool aside: it
#: writes that one person's memory), never the agent's apps or checks, never
#: a machine's controls or the web by another name; its record tool rides it.
WRAPUP_RULE = KindRule(
    denied=(EXTERNAL_DENIED_MCPS - {"memory-mcp", CONVERSATIONS_MCP})
    | {"display-mcp", "checks-mcp", APPS_MCP} | MACHINE_CONTROL_MCPS | WEB_REACHING_MCPS,
    reason="Not available after a conversation",
    admitted=frozenset({CONVERSATIONS_MCP}),
)

def wrapup_rule(*, person: bool) -> KindRule:
    """The wrap-up's rule; without a person (a withheld number, a Shared-only
    agent's caller, the agent's own line) no memory is written either."""
    if person:
        return WRAPUP_RULE
    return KindRule(denied=WRAPUP_RULE.denied | {"memory-mcp"}, reason=WRAPUP_RULE.reason,
                    admitted=WRAPUP_RULE.admitted)


#: The management tools a route on the agent's own line may tick, one by
#: one (placing calls, a machine's controls and the agent's memory never:
#: anyone who dials acts as the agent).
AGENT_LINE_TICKABLE = EXTERNAL_DENIED_MCPS - {"phone-mcp", "memory-mcp"}


def agent_line_rule(ticks) -> KindRule:
    """The agent's own phone line: a caller runs as the agent itself, with
    the management tools off but those the route ticks."""
    opened = frozenset(ticks or ()) & AGENT_LINE_TICKABLE
    return KindRule(
        denied=(EXTERNAL_DENIED_MCPS | {CONVERSATIONS_MCP} | MACHINE_CONTROL_MCPS) - opened,
        reason="Not available on this phone line",
        open_line=True,
    )


#: The words a browser that reaches the local network is left out with.
OPEN_EXIT_REASON = "Reaches the local network, so outside people do not get it"

# The ``network_exit`` MCPs whose local network is open (the approval on):
# outside people's sessions and a session kind's own (the agent's line, the
# wrap-up) never get one, as one shared container serves every session. Kept
# in memory, set at boot (``note_open_exits``) and by the approval's route.
_open_exits: frozenset[str] = frozenset()


def note_open_exits(names) -> None:
    global _open_exits
    _open_exits = frozenset(names)


def note_open_exit(name: str, is_open: bool) -> None:
    global _open_exits
    _open_exits = (_open_exits | {name}) if is_open else (_open_exits - {name})


def session_exclusion_reason(
    manifest, *, contexts: set[str] | frozenset[str], external: bool,
    picks: OutsidePicks | None = None, kind_rule: KindRule | None = None,
    limits: Mapping[str, Any] | None = None,
) -> str | None:
    """Why ``manifest`` is NOT part of a session with these ``contexts``
    (``{"phone"}``, ``{"task", "meeting"}``, …) — or None when it is.

    THE one rule every consumer applies: the session MCP config
    (``build_session_mcp_config``), the prompt catalog, the inline skills
    and the Direct-LLM skill catalog, the route preview, and the layers'
    per-MCP dir binds. ``external`` adds the ``"external"`` context AND the
    :data:`EXTERNAL_DENIED_MCPS` hard set, and admits nothing a manager did
    not pick for outside people (``picks``, ``services/mcp/outside_picks``;
    the channel's own tools count as picked). A reader that passes
    ``external`` without the picks admits nothing: fail closed. So prompt,
    config, key material and processes can never disagree about what an
    outside person gets. ``kind_rule`` is a session kind's own hard set and
    the platform tools it rides with (the wrap-up, the agent's own line).
    ``limits`` are the agent's tool limits for this session's kind
    (``services/mcp/tool_limits``, by manifest name): a server whose limit
    grants no function is left out with the limit's reason; a reader that
    passes none admits the server, since a chat's or a run's default is no
    limit.
    """
    if manifest.name in tool_envs.stale_names():
        # The boot's scan found its environment not built, and the server
        # builds none (``mcp_venv_bootstrap``).
        return tool_envs.stale_reason(manifest.label or manifest.name)
    if external and manifest.name in EXTERNAL_DENIED_MCPS:
        return EXTERNAL_DENIED_REASON
    if (external or kind_rule is not None) and manifest.name in _open_exits:
        return OPEN_EXIT_REASON
    if kind_rule is not None:
        if manifest.name in kind_rule.denied:
            return kind_rule.reason
        if manifest.name in kind_rule.admitted:
            return None
    effective = set(contexts) | ({EXTERNAL_CONTEXT} if external else set())
    exclude_from = manifest.exclude_from or []
    # "meeting" outranks the base context (a manifest may opt out of
    # meetings alone); the external context is checked last so a manifest
    # listing both "phone" and "external" reports the base reason.
    ordered = (
        [c for c in ("meeting",) if c in effective]
        + sorted(effective - {"meeting", EXTERNAL_CONTEXT})
        + ([EXTERNAL_CONTEXT] if EXTERNAL_CONTEXT in effective else [])
    )
    for c in ordered:
        if c and c in exclude_from:
            return f"Excluded in {c} mode"
    if external and (picks is None or not picks.admits(manifest.name)):
        return NOT_PICKED_REASON
    if limits and manifest.name in limits:
        from services.mcp import tool_limits
        return tool_limits.exclusion_reason(limits[manifest.name], tool_limits.server_key(manifest))
    return None


def filter_manifests_for_session(
    manifests, *, contexts: set[str] | frozenset[str], external: bool,
    picks: OutsidePicks | None = None,
) -> list:
    """``manifests`` minus those :func:`session_exclusion_reason` drops."""
    return [
        m for m in manifests
        if session_exclusion_reason(m, contexts=contexts, external=external, picks=picks) is None
    ]


def preview_session_mcps(
    agent_name: str, *, contexts: set[str], external: bool,
    picks: OutsidePicks | None = None, kind_rule: KindRule | None = None,
) -> tuple[list[McpManifest], dict[str, str]]:
    """Manifest-only view of what a session with ``contexts`` would attach:
    the agent's configured MCPs minus what :func:`session_exclusion_reason`
    drops. Returns ``(attached, excluded_reasons)``.

    No processes, no credential resolution — the admin route form uses it to
    show "available to callers on this line" before saving; the session
    builder applies the same rule (plus credential availability).
    """
    attached: list[McpManifest] = []
    excluded: dict[str, str] = {}
    for manifest in get_agent_mcps_all_placements(agent_name):
        reason = session_exclusion_reason(manifest, contexts=contexts, external=external, picks=picks,
                                          kind_rule=kind_rule)
        if reason:
            excluded[manifest.name] = reason
            continue
        attached.append(manifest)
    return attached, excluded


def audience_admits(audience: str, user_role: str | None) -> bool:
    """Whether a session at ``user_role`` is in ``audience`` (the tier names
    of ``auth/roles.py``; an empty audience admits everyone). ``None`` is
    "no person known": the caller did not say, nothing is filtered."""
    if user_role is None or not audience:
        return True
    from auth import roles
    gate = {
        "owner": roles.can_manage,
        "editor": roles.can_edit,
        "workspace": roles.can_write_workspace,
    }.get(audience)
    return bool(gate and gate(user_role))


def skill_audience_admits(skill_def, user_role: str | None) -> bool:
    """:func:`audience_admits` for a skill's ``audience``."""
    return audience_admits(getattr(skill_def, "audience", ""), user_role)


def audience_refusal(manifest, *, user_role: str | None, task_scope: str) -> str | None:
    """Why a manifest with an ``audience`` is not part of this session, or
    None: a person below the tier in a user-scope session. An agent-scope
    session is the agent acting as itself and is admitted; a builder that
    names no role (an empty string) is "no person known" and admitted, as
    the skills' gate reads it."""
    if not getattr(manifest, "audience", "") or task_scope != "user":
        return None
    if audience_admits(manifest.audience, user_role or None):
        return None
    return f"{manifest.label} is for the {manifest.audience} tier of this agent and above"


def _exclusion_contexts(phone_mode: bool, task_mode: bool, interactive_local: bool,
                        meeting_mode: bool, external: bool, *,
                        shared_chat: bool = False, chat_worker: bool = False) -> set[str]:
    """The ``exclude_from`` contexts a session matches (its kind, ``meeting``
    besides it for a participant, ``external`` for a caller, ``shared_chat``
    for a session that is not the person's own chat; a person's own chat
    lane matches the chat's kind, not ``task``)."""
    # Determine context for exclusion checks
    context = "dashboard"
    if phone_mode:
        context = "phone"
    elif task_mode and not chat_worker:
        context = "task"
    elif interactive_local:
        # Interactive terminal: a local CLI session (Claude Code / Codex on the
        # user's own machine) — exclude device/UI MCPs that make no sense there
        # (location GPS, display image-viewer; see their manifests' exclude_from).
        context = "terminal"
    # Meeting sessions match "meeting" IN ADDITION to their base context
    # ("task" — meetings ride the task lane), so a manifest can opt out of
    # meetings alone. Kept as a union, never a replacement: manifests that
    # exclude only "task" have always been absent from meetings, and swapping
    # the context would silently re-admit them all.
    exclusion_contexts = {context, "meeting"} if meeting_mode else {context}
    # External sessions (a phone caller who is not a platform user) add the
    # "external" context AND the hard-denied management set — see
    # ``session_exclusion_reason``; the prompt builders apply the same rule.
    if external:
        exclusion_contexts.add(EXTERNAL_CONTEXT)
    if shared_chat:
        exclusion_contexts.add(SHARED_CHAT_CONTEXT)
    return exclusion_contexts


def _prompt_contexts(context: str, shared_chat: bool) -> set[str]:
    """The contexts a prompt-side filter matches: the session's kind word
    and, for a session that is not the person's own chat, ``shared_chat``
    (the same set the config build matched, so the two agree)."""
    contexts = {context} if context else set()
    if shared_chat:
        contexts.add(SHARED_CHAT_CONTEXT)
    return contexts
