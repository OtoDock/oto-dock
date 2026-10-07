"""The role vocabulary: the platform role, the per-agent role and the
effective role, named once (core-seams phase 5).

Three closed sets the store spells the same way (``users.role``,
``user_agents.agent_role``, ``phone_routes.role``; the CHECKs are the
frozen spellings), the rank an app action floor is judged by, the tiers
the mounts and the path policy key on, and the questions generic code
asks. Nothing here reads a store: the principal module
(``auth/providers``) carries the store-backed pair ``effective_role_of``
/ ``acting_role_of``, and ``UserContext`` delegates its ``can_*`` here.

A leaf: stdlib only, importable from ``config``, ``storage`` and the
sandbox without a cycle. Constants and tuples, not an ``Enum`` (the
repo's grain). The dashboard mirror is ``lib/permissions.ts``;
``tests/auth/test_roles.py`` keeps it in lock-step and
``tests/auth/test_roles_surface.py`` keeps a member spelling out of every
other module's branches.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

# The platform role (``users.role``): what a person may do platform-wide.
ADMIN = "admin"
CREATOR = "creator"
MEMBER = "member"
PLATFORM_ROLES = (ADMIN, CREATOR, MEMBER)     # the CHECK's order

# The per-agent role (``user_agents.agent_role``, ``phone_routes.role``):
# what a person may do on ONE agent.
MANAGER = "manager"
EDITOR = "editor"
CONTRIBUTOR = "contributor"
VIEWER = "viewer"
AGENT_ROLES = (MANAGER, EDITOR, CONTRIBUTOR, VIEWER)       # the CHECK's order

# The in-memory platform role of a principal that is no person: a session
# token minted with no user (a phone call, a trigger, an agent-scope task,
# a meeting service). Never persisted; clears no tier and no floor.
SERVICE = "agent"

# The effective role of a principal on an agent that holds no row for it.
NO_ACCESS = ""

# The tiers the mounts, the path policy and the file API key on. Spelled
# out in words (not through the constants above) so a row reads at a
# glance; ``tests/auth/test_roles.py`` binds them to the constants. An MCP
# process carries no copy: the proxy answers the tier questions in its env
# (``core/sandbox/oto_env.py``, the ``OTO_CAN_*`` flags).
OWNER_TIER = ("manager", "admin")             # curates config and knowledge
EDITOR_TIER = ("manager", "editor", "admin")  # automates as the agent, manages its apps
WORKSPACE_TIER = ("manager", "editor", "contributor", "admin")  # writes the shared workspace
CREATOR_TIER = ("admin", "creator")           # creates agents, reaches the creator surfaces
# The per-agent roles a Shared-only agent's rows may hold: every session
# there runs as the agent, which takes the editor tier, so a lower role
# would be an assignment that can open no chat.
SHARED_ONLY_ROLES = ("manager", "editor")

# The effective roles by rank: the order an action floor (``min_role``)
# is judged in. A word outside the table (NO_ACCESS, a claim's ``none``,
# a spelling nobody minted) ranks 0: it clears the viewer floor and no
# other, which is how the app broker lets a stranger reach an app's
# default floor on the edge alone.
EFFECTIVE_ROLES = (VIEWER, CONTRIBUTOR, EDITOR, MANAGER, ADMIN)
RANK = {role: index for index, role in enumerate(EFFECTIVE_ROLES)}
# The platform roles by rank: the order a route gate and the OIDC fold use.
PLATFORM_BY_RANK = (MEMBER, CREATOR, ADMIN)
PLATFORM_RANK = {role: index for index, role in enumerate(PLATFORM_BY_RANK)}


def is_admin(role: str | None) -> bool:
    """A platform admin (the cookie admin and the master key alike)."""
    return role == ADMIN


def is_creator_or_above(role: str | None) -> bool:
    return role in CREATOR_TIER


def can_manage(role: str | None) -> bool:
    """The owner tier: config, MCP wiring, knowledge curation, bindings,
    the approvals that hand an agent's service accounts to a script."""
    return role in OWNER_TIER


def can_edit(role: str | None) -> bool:
    """The editor tier: acts under the agent's identity — own agent-scope
    automations, continuing an agent-scope task, the shared apps. Every
    editor also writes the shared workspace (``can_write_workspace``)."""
    return role in EDITOR_TIER


def can_write_workspace(role: str | None) -> bool:
    """The workspace tier: writes the shared workspace (the mount, the path
    policy, the file API, uploads, a paired machine's write-back) and
    nothing under the agent's identity — the contributor's whole grant."""
    return role in WORKSPACE_TIER


def allowed_on_shared_only(role: str | None) -> bool:
    """Whether an assignment row of ``role`` may be written on a
    Shared-only agent (``SHARED_ONLY_ROLES``)."""
    return role in SHARED_ONLY_ROLES


def rank(role: str | None) -> int:
    return RANK.get(role or "", 0)


def meets_floor(role: str | None, floor: str | None) -> bool:
    """Whether ``role`` clears an action floor; an empty floor is the
    viewer floor (the default, never stored in a signed manifest). A floor
    the table does not know is unreachable — the manifest parsers refuse
    one at deploy, so none is stored; this keeps a typo from opening an
    action to everyone."""
    wanted = floor or VIEWER
    return wanted in RANK and rank(role) >= RANK[wanted]


def capped(role: str, cap: str) -> str:
    """The lower of ``role`` and ``cap`` by rank (a phone line caps the
    tied user at manager: never the admin policy)."""
    return role if rank(role) <= rank(cap) else cap


def highest_platform_role(roles: Iterable[str | None]) -> str | None:
    """The strongest platform role among ``roles`` (the OIDC group fold);
    None when none is a platform role."""
    best: str | None = None
    for role in roles:
        if role in PLATFORM_RANK and (best is None or PLATFORM_RANK[role] > PLATFORM_RANK[best]):
            best = role
    return best


def effective_role(platform_role: str | None, agent_roles: Mapping[str, str] | None, agent: str) -> str:
    """The one answer for a principal on an agent: ADMIN for a platform
    admin, else the per-agent row, else NO_ACCESS: the truthful reading a
    membership check wants (an owner re-checked from a stored sub, a task
    fire's provenance, the otodock TUI's owner)."""
    if is_admin(platform_role):
        return ADMIN
    return (agent_roles or {}).get(agent) or NO_ACCESS


def row_role(agent_roles: Mapping[str, str] | None, agent: str) -> str:
    """The per-agent row alone, the platform role ignored: what a bearer
    principal presents to an app, what a user-paired machine syncs as, what
    a task's delivery rung resolves its target with. NO_ACCESS without a
    row. A deliberate reading, named so the sites that take it say so."""
    return (agent_roles or {}).get(agent) or NO_ACCESS


def acting_role(platform_role: str | None, agent_roles: Mapping[str, str] | None, agent: str) -> str:
    """The role an ADMITTED principal acts with: a session on the agent it
    was minted for, a share grantee, a stranger an app edge lets in. VIEWER
    when it holds no row; ``effective_role`` otherwise. The floor, not a
    second sentinel: every caller sits behind an admission gate that
    refused a stranger already."""
    return effective_role(platform_role, agent_roles, agent) or VIEWER


def may_mutate_shared(role: str | None, *, own: bool) -> bool:
    """The agent-scope mutation ladder for tasks, triggers and their chats:
    the owner tier mutates any, an editor its own, a viewer nothing. The
    user-scope half (the creator; for chats the admin too) stays at the
    caller, which asks the scope axis."""
    if can_manage(role):
        return True
    return role == EDITOR and own


def label(role: str | None) -> str:
    """The word a prompt or a badge shows: any member as it is; a spelling
    outside them (NO_ACCESS, SERVICE, a stranger's word) reads viewer."""
    return role if role in RANK or role in PLATFORM_RANK else VIEWER
