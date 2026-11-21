"""A chat's choice of account per tool as a session takes it, and the
prompt's block of the accounts a chat may switch between.

Two kinds of chat choose. A person's own chat chooses among the accounts
that person lets the agent use (their set, ``chat_account_choices``). A
dashboard chat whose session runs in the agent's space chooses among the
agent's own accounts (the agent's set, ``chat_service_account_choices``),
for everyone who drives it, in every mode. Whose accounts a person's chat
runs on is ``people_use_agents_accounts``: the agent's own on a Shared-only
agent (the visibility rule: every person's chat runs in its space) and on a
collaborative agent whose team account is on (``team_account``), from its
role floor up (``team_floor_refusal``); the person's own elsewhere. Every
other session (a task, a call, a meeting, a delegated worker, an outside
person) takes no choice and runs on the default (``choice_scope``, the one
gate).

``credential_identity`` is the one answer to whose credentials a session
resolves: the session build asks it once and every credential it reads
follows (the resolver, the gateway's bearer, the owner filter of a personal
machine, the network a tool may reach). ``whose_accounts_line`` names the
person whose accounts a session runs on when they are not the agent's own.

A session on a machine takes the choice as one on the platform does.

The block is for a dashboard chat that chooses, whose agent has the
``switch_account`` tool: one line for each tool whose set holds more than
one account, naming the account the chat uses and the others it may use.
Labels and addresses only: no credential value reaches the prompt. The
builders append it (``account_lines``) when ``choice_scope`` answers.

``reader_scope`` names the identity every other reader of a session's
account resolves in (the prompt's account tokens, a builder block's bearer,
the network a tool may reach), so they name the account the tools run on.
"""

import dataclasses
import logging
from collections.abc import Mapping

from auth import roles
from core.session.visibility import SCOPE_AGENT, SCOPE_USER

logger = logging.getLogger("otodock")

#: The MCP that carries ``switch_account``: a session without it cannot
#: switch, so it is told of no other account.
AGENT_CONFIG_MCP = "agent-config-mcp"

#: Why a Personal-only agent's accounts are neither read nor written.
PERSONAL_ONLY = "This agent is Personal only. It has no accounts of its own."

HEADING = "## Accounts you may switch to"
WHOSE_HEADING = "## Whose accounts this chat uses"

#: A person below the team account's floor: every per-user tool is left out
#: of their sessions with this sentence.
TEAM_FLOOR = ("The agent's own accounts are for the {floor} role and above on this agent. "
              "Ask a manager for that role.")

# A label or an address as the prompt carries it (a label is the person's
# own text, an address the vendor's).
_WORD_CAP = 120


def holds_own_accounts(agent_name: str) -> bool:
    """Whether the agent has accounts of its own: every mode with a space of
    the agent's, so not Personal only (``PERSONAL_ONLY``). Synchronous, a
    read of the agents' cache when it is warm."""
    from core.session.visibility import available_scopes_for
    from storage.agents import agent_store
    row = agent_store.get_agent(agent_name) or {}
    return SCOPE_AGENT in available_scopes_for(
        bool(row.get("collaborative", True)), row.get("default_scope") or SCOPE_USER)


def people_use_agents_accounts(agent_name: str) -> bool:
    """Whether a person's session on this agent runs on the agent's own
    accounts: a Shared-only agent (the visibility rule: every person's chat
    runs in its space, ``visibility.is_shared_only``), or a collaborative one
    whose team account is on (``team_account``; a stored switch on an agent
    that is no longer collaborative counts for nothing). The one place the
    mode and the switch are read for accounts. Synchronous, a read of the
    agents' cache when it is warm."""
    from core.session.visibility import is_shared_only
    from storage.agents import agent_store
    if is_shared_only(agent_name):
        return True
    row = agent_store.get_agent(agent_name) or {}
    return bool(row.get("team_account")) and bool(row.get("collaborative"))


def team_account(agent_name: str) -> tuple[bool, str]:
    """The agent's team account switch and its role floor, ``(on, floor)``,
    as the agents row holds them (the route keeps the switch off on an
    agent without both a shared and a personal space). Synchronous, a read
    of the agents' cache when it is warm."""
    from storage.agents import agent_store
    row = agent_store.get_agent(agent_name) or {}
    return bool(row.get("team_account")), str(row.get("team_account_floor") or roles.EDITOR)


def team_floor_refusal(agent_name: str, role: str) -> str:
    """The sentence for a person of ``role`` below the team account's floor
    on an agent whose switch is on (``TEAM_FLOOR``), ``""`` otherwise: a role
    nobody knows ranks as a viewer (``roles.rank``), so it is below every
    floor but the viewer's. Synchronous (the agents' cache)."""
    on, floor = team_account(agent_name)
    if not on or roles.meets_floor(role, floor):
        return ""
    return TEAM_FLOOR.format(floor=floor)


@dataclasses.dataclass(frozen=True)
class Identity:
    """Whose credentials a session resolves: ``sub`` and ``scope`` as the
    resolver takes them (the person's sub at user scope, None at agent
    scope), and the sentence for a person below the team account's floor
    (``team_floor_refusal``: every per-user tool is left out with it), ``""``
    otherwise."""
    sub: str | None
    scope: str
    refusal: str = ""

    @property
    def agents(self) -> bool:
        return self.scope == SCOPE_AGENT


def credential_identity(agent_name: str, user_sub: str | None, *, mount_scope: str,
                        role: str = "") -> Identity:
    """The identity a session's credentials resolve in. No person, or the
    agent's own mount: the agent's. A person at their own mount on an agent
    whose people use their own accounts: theirs. On one whose people use the
    agent's accounts: the agent's, from the floor up, and the agent's with
    the floor's sentence below it. Synchronous (the agents' cache)."""
    if not user_sub or mount_scope == SCOPE_AGENT:
        return Identity(None, SCOPE_AGENT)
    if not people_use_agents_accounts(agent_name):
        return Identity(user_sub, SCOPE_USER)
    return Identity(None, SCOPE_AGENT, team_floor_refusal(agent_name, role))


def reader_scope(agent_name: str, user_sub: str | None) -> tuple[str, str]:
    """The identity a reader of a session's account resolves in: the
    person's own (``("user", user_sub)``) for a session with a person on an
    agent whose people use their own accounts, the agent's own
    (``("agent", "")``) otherwise. No role: below the floor every per-user
    tool is left out of the session, so no reader of one runs. Synchronous,
    and a read of the agents' cache when it is warm: call off the loop where
    a cold cache would cost."""
    if user_sub and not people_use_agents_accounts(agent_name):
        return SCOPE_USER, user_sub
    return SCOPE_AGENT, ""


def _chat_of_kind(chat_id: str, kind) -> dict | None:
    """The chat's row when it is of session kind ``kind`` (``session_kind``)
    on an agent, else None. Synchronous store read."""
    from core.session import session_kind
    from storage import database as task_store
    if not chat_id or (kind is session_kind.DASHBOARD and session_kind.is_task_chat_id(chat_id)):
        return None
    chat = task_store.get_chat(chat_id)
    if not chat or not chat.get("agent") or session_kind.of_chat(chat) is not kind:
        return None
    return chat


def choice_scope(chat_id: str, *, mount_scope: str, role: str = "", task: bool = False) -> str:
    """Whose choice of account a session of this chat takes: ``"agent"`` on
    an agent whose people use its accounts (``people_use_agents_accounts``),
    for every dashboard chat and a task's own chat (``task``: the session's
    kind; a chat-surface worker's is a dashboard chat its round works in),
    except below the
    team account's floor; on an agent whose people use their own, ``"agent"``
    for the agent's pool chat and a task's own chat at the agent's mount,
    ``"user"`` at a person's; ``""`` for a chat that continues delegated work
    (its rounds keep the default), a call's chat, a meeting's and no chat at
    all. Synchronous store reads: call off the loop."""
    from core.session import session_kind
    from core.session.visibility import is_synthetic_owner, shared_chat_owner
    chat = _chat_of_kind(chat_id, session_kind.TASK if task else session_kind.DASHBOARD)
    if chat is None and task:
        # A chat-surface worker's chat is a dashboard chat a round works in.
        from services.remote import chat_move
        chat = _chat_of_kind(chat_id, session_kind.DASHBOARD)
        if chat is not None and not chat_move._continues_delegated_work(chat):
            chat = None
    if chat is None:
        return ""
    agent = chat["agent"]
    owner = chat.get("user_sub") or ""
    pool = owner == shared_chat_owner(agent)
    if not task:
        from services.remote import chat_move
        if (not pool and is_synthetic_owner(owner)) or chat_move._continues_delegated_work(chat):
            return ""
    if people_use_agents_accounts(agent):
        return "" if team_floor_refusal(agent, role) else SCOPE_AGENT
    if mount_scope != SCOPE_AGENT:
        return SCOPE_USER
    return SCOPE_AGENT if pool or task else ""


def chooses_agents_accounts(chat: dict, *, role: str = "") -> bool:
    """Whether a switch of this dashboard chat chooses among the agent's own
    accounts: its agent's people use them (``people_use_agents_accounts``)
    and ``role`` clears the team account's floor (elsewhere a person driving
    the agent's pool chooses among their own). Synchronous store reads: call
    off the loop."""
    from core.session import session_kind
    agent = chat.get("agent") or ""
    if not agent or not people_use_agents_accounts(agent) or team_floor_refusal(agent, role):
        return False
    return _chat_of_kind(chat.get("id") or "", session_kind.DASHBOARD) is not None


def session_choices(chat_id: str, person: str, *, mount_scope: str = SCOPE_USER,
                    role: str = "", task: bool = False) -> dict[str, str]:
    """The chat's choices of account a session of ``person`` at
    ``mount_scope`` takes (``{mcp_name: account_label}``): a chat that chooses
    among the agent's own accounts its own, a person's own chat the choices
    of that person, none for any other chat (``choice_scope``). Synchronous
    store reads: call off the loop."""
    if not chat_id:
        return {}
    scope = choice_scope(chat_id, mount_scope=mount_scope, role=role, task=task)
    if scope == SCOPE_AGENT:
        from storage.identity import service_account_set
        return service_account_set.get_chat_service_choices(chat_id)
    if scope != SCOPE_USER or not person:
        return {}
    from storage.identity import credential_store
    return credential_store.get_chat_account_choices(chat_id, person)


def account_lines(
    agent_name: str, assigned_mcps: list[str], *, user_sub: str,
    account_choices: Mapping[str, str] | None = None,
    scope: str = SCOPE_USER, run: bool = False,
) -> str:
    """The block for one session, or ``""``: whose accounts the session runs
    on when they are a person's (``whose_accounts_line``, ``run`` for a
    task's), then the accounts it may switch between. ``scope`` is the
    chat's (``choice_scope``): ``"user"`` lists the person's own sets,
    ``"agent"`` the agent's own sets, ``""`` none. No switch line with no
    ``agent-config-mcp`` among the assigned MCPs, on an engine a choice does
    not reach, or with no tool whose set holds more than one account. The
    account in use is the one ``credential_resolver.pick_account`` picks
    with ``account_choices`` (the session's own, ``session_choices``), the
    others follow in the set's order. Synchronous store reads: call off the
    loop."""
    try:
        whose = whose_accounts_line(agent_name, user_sub, assigned_mcps, account_choices, run=run)
        lines = (_switch_lines(agent_name, assigned_mcps, user_sub, account_choices, scope,
                               attended=not run)
                 if AGENT_CONFIG_MCP in assigned_mcps else [])
    except Exception as e:  # noqa: BLE001  # the block spans the store and the resolver, the prompt goes on
        logger.warning("accounts block for %s not built: %s", agent_name, e)
        return ""
    if lines:
        return "\n".join([HEADING, "", *([whose, ""] if whose else []), *lines])
    if whose:
        return "\n".join([WHOSE_HEADING, "", whose])
    return ""


def _switch_lines(agent_name: str, assigned_mcps: list[str], user_sub: str,
                  account_choices: Mapping[str, str] | None, scope: str, *,
                  attended: bool = True) -> list[str]:
    """One line per tool the chat may switch, for the set ``scope`` names;
    ``attended`` False for a run, whose line says how its switch lands."""
    agents_own = scope == SCOPE_AGENT
    if not agents_own and (scope != SCOPE_USER or not user_sub
                           or people_use_agents_accounts(agent_name)):
        return []
    lines: list[str] = []
    for mcp_name in assigned_mcps:
        line = (_agents_tool_line(mcp_name, agent_name, account_choices, attended=attended)
                if agents_own
                else _tool_line(mcp_name, agent_name, user_sub, account_choices, attended=attended))
        if line:
            lines.append(line)
    return lines


#: How a line ends: a chat asks the person first; a run goes on by itself.
_SWITCH_TAIL = "To switch this chat, ask the person, then call `switch_account`."
_RUN_TAIL = ("To switch this run to another account, call `switch_account` and finish your turn: "
             "the run goes on with it from your next round, in the same chat. Only these accounts "
             "are offered, and a run that drives a terminal cannot switch while it runs.")


def whose_accounts_line(
    agent_name: str, user_sub: str | None, assigned_mcps: list[str],
    account_choices: Mapping[str, str] | None = None, *, run: bool = False,
) -> str:
    """One sentence naming the person whose accounts the session runs on
    and, per attached tool that is a person's account with one picked, the
    account; ``""`` when the identity is the agent's own (``reader_scope``)
    or no attached tool has an account. ``run``: a task's session.
    Synchronous store reads: call off the loop."""
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver
    scope, person = reader_scope(agent_name, user_sub)
    if scope != SCOPE_USER:
        return ""
    named: list[str] = []
    for mcp_name in assigned_mcps:
        manifest = mcp_registry.get_manifest(mcp_name)
        creds = getattr(manifest, "credentials", None)
        if manifest is None or getattr(creds, "type", "") != credential_resolver.PER_USER:
            continue
        ref = credential_resolver.pick_account(
            mcp_name, agent_name, user_sub=person, **credential_resolver.choice_kw(account_choices))
        if ref is None:
            continue
        named.append(f"{word(str(manifest.label or mcp_name))} on "
                     f"{named_account(person, mcp_name, ref.label)}")
    if not named:
        return ""
    name = person_name(person)
    where = "this run" if run else "this chat"
    return (f"The accounts in {where} are {name}'s own: {', '.join(named)}. "
            f"What you send through them goes out as {name}.")


def person_name(sub: str) -> str:
    """How a platform sentence names a person: their display name, else
    their username. Synchronous store read."""
    from storage import database as task_store
    row = task_store.get_user(sub) or {}
    return word(str(row.get("display_name") or row.get("name") or row.get("username") or "Someone"))


def _tool_line(
    mcp_name: str, agent_name: str, user_sub: str, account_choices: Mapping[str, str] | None,
    *, attended: bool = True,
) -> str:
    """One tool's line, or ``""`` for a tool that is not a person's own
    account or whose set for the agent holds one account or none."""
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver
    from storage.identity import credential_store
    manifest = mcp_registry.get_manifest(mcp_name)
    creds = getattr(manifest, "credentials", None)
    if manifest is None or getattr(creds, "type", "") != credential_resolver.PER_USER:
        return ""
    accounts = credential_store.list_agent_account_set(user_sub, mcp_name, agent_name)
    if len(accounts) < 2:
        return ""
    ref = credential_resolver.pick_account(
        mcp_name, agent_name, user_sub=user_sub,
        **credential_resolver.choice_kw(account_choices),
    )
    in_use = ref.label if ref is not None else accounts[0].account_label
    # The address ``dynamic_context._build_token_map`` reads for
    # ``${account.email}``: the account row's ``display_email``.
    addresses = {
        str(a["account_label"]): str(a.get("display_email") or "")
        for a in credential_store.list_user_accounts(user_sub, mcp_name)
    }
    tool = word(str(manifest.label or mcp_name))
    this = "this chat" if attended else "this run"
    # The tool's own name follows its label: it is what ``switch_account`` takes.
    head = f"- {tool} (`{mcp_name}`): {this} uses {_named(in_use, addresses)}."
    others = [_named(a.account_label, addresses) for a in accounts if a.account_label != in_use]
    return f"{head} You may also use: {', '.join(others)}. {_SWITCH_TAIL if attended else _RUN_TAIL}"


def _agents_tool_line(
    mcp_name: str, agent_name: str, account_choices: Mapping[str, str] | None,
    *, attended: bool = True,
) -> str:
    """One tool's line in a chat on the agent's accounts, or ``""`` for a tool
    that is not a person's account or whose agent's set holds one account or
    none. A choice whose account left the set names none in use, and so does
    a default whose lender no longer stands."""
    from services.mcp import mcp_registry
    from services.oauth import credential_resolver
    from storage.identity import credential_store, service_account_set
    manifest = mcp_registry.get_manifest(mcp_name)
    creds = getattr(manifest, "credentials", None)
    if manifest is None or getattr(creds, "type", "") != credential_resolver.PER_USER:
        return ""
    accounts = service_account_set.list_service_agent_set(mcp_name, agent_name)
    if len(accounts) < 2:
        return ""
    ref = credential_resolver.pick_account(
        mcp_name, agent_name, **credential_resolver.choice_kw(account_choices))
    addresses: dict[str, str] = {}
    for owner in {a.account_owner_sub for a in accounts}:
        for row in credential_store.list_user_accounts(owner, mcp_name):
            if any(a.account_owner_sub == owner and a.account_label == row["account_label"]
                   for a in accounts):
                addresses[str(row["account_label"])] = str(row.get("display_email") or "")
    tool = word(str(manifest.label or mcp_name))
    this = "this chat" if attended else "this run"
    if ref is None:
        gone = (f"the account {this} chose is no longer one of the agent's"
                if credential_resolver.chosen_account_gone(mcp_name, agent_name, account_choices)
                else "the agent's default account for it is not in use")
        head = f"- {tool} (`{mcp_name}`): {gone}."
        others = [_named(a.account_label, addresses) for a in accounts]
    else:
        head = f"- {tool} (`{mcp_name}`): {this} uses {_named(ref.label, addresses)}."
        others = [_named(a.account_label, addresses) for a in accounts if a.account_label != ref.label]
    if not attended:
        return (f"{head} You may also use: {', '.join(others)}. These are the agent's own "
                f"accounts. {_RUN_TAIL}")
    return (f"{head} You may also use: {', '.join(others)}. These are the agent's own "
            "accounts, and a switch is for everyone in this chat. To switch it, ask the "
            "person, then call `switch_account`.")


def named_account(owner_sub: str, mcp_name: str, label: str) -> str:
    """How a line names one person's account of a tool: its label, with its
    address when it has one. Synchronous store read: call off the loop."""
    from storage.identity import credential_store
    rows = credential_store.list_user_accounts(owner_sub, mcp_name)
    return _named(label, {str(r["account_label"]): str(r.get("display_email") or "") for r in rows})


def _named(label: str, addresses: dict[str, str]) -> str:
    """``label (address)``, or the label alone when the account has no
    address or its label is the address."""
    address = addresses.get(label, "")
    if not address or address == label:
        return word(label)
    return f"{word(label)} ({word(address)})"


def word(text: str) -> str:
    """A label, a tool's name or a machine's name as a prompt and the
    platform's own sentences carry it: one line, no backtick."""
    from services.mcp.dynamic_context import _prompt_word
    return _prompt_word(text, _WORD_CAP)
