"""External identities — who a caller IS when they are not a platform user.

An EXTERNAL session is a phone call (later: any external channel — a website
chatbot, for instance) whose principal is not a platform user. The route's
``caller`` identity mode gives every caller a private space keyed on their
normalised id; the ``shared`` mode gives the agent's shared space to an
id-less external principal. This module owns the identity rules — the
normalisation, the withheld / ephemeral case, the JWT ``ext`` claim and its
inverse back to the on-disk home — so the warmup, the memory API, the
sandbox and the retention sweep can never disagree about where a caller's
data lives.

Channel-agnostic by construction: ``channel`` is a parameter and the on-disk
layout is ``agents/<slug>/externals/<channel>/<caller slug>/`` with the same
subdirectories a user dir has (``workspace/``, ``context/`` with the memory
at ``context/memory/``, ``.claude/``, ``.codex/``). Inside the session the
tree is mounted at :data:`SANDBOX_HOME` — one fixed name, because a session
only ever sees its own caller.

Assurance: a phone caller-id is what the trunk reports — spoofable on SIP /
PSTN. The identity carries ``verified`` (the daemon's PIN gate passed) so the
prompt and the call log can say how much to trust it; anything that is not
digits is treated as withheld rather than sanitised into a colliding slug.
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("otodock")

#: Agent-tree subdirectory holding every external caller space.
EXTERNALS_DIRNAME = "externals"
#: Sub-tree for callers with no durable identity (withheld number, or a route
#: that does not remember callers) — keyed by the session id, pruned at
#: teardown, never listed or synced.
EPHEMERAL_DIRNAME = "_ephemeral"
#: Where the caller's own tree appears inside the session (every channel).
SANDBOX_HOME = "/caller"
#: The phone channel key (the only channel today).
PHONE = "phone"

_CHANNEL_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
# A phone id: optional leading "+", then 2–20 digits. Formatting separators
# (spaces, dashes, dots, parentheses) are dropped first; anything else — a
# name, "anonymous", an extension label — is WITHHELD, never sanitised.
_PHONE_ID_RE = re.compile(r"^\+?\d{2,20}$")
_PHONE_SEPARATORS = re.compile(r"[ \-().]")
_WITHHELD = frozenset({
    "", "anonymous", "unknown", "restricted", "private", "withheld",
    "unavailable", "blocked",
})
_EPHEMERAL_PREFIX = "ephemeral:"


@dataclass(frozen=True)
class ExternalIdentity:
    """One resolved external principal for one session."""

    channel: str
    #: Normalised id ("+30210…" for phone); "" when withheld or when the
    #: route runs in the shared mode.
    id: str
    #: Path component under ``externals/<channel>/``; "" when the session has
    #: no caller tree (shared mode, or a mode without a personal scope).
    slug: str
    #: The tree lives under ``_ephemeral/<session_id>`` and is pruned at
    #: teardown (withheld id, or ``remember_callers`` off on the route).
    ephemeral: bool
    #: The daemon's PIN gate passed for this call (honoured only when the
    #: route actually has a PIN — the warmup resolver checks that).
    verified: bool
    #: The JWT ``ext`` value: ``"<channel>:<id>"``, id-less ``"<channel>:"``
    #: or ``"<channel>:ephemeral:<session_id>"``.
    claim: str

    @property
    def has_tree(self) -> bool:
        return bool(self.slug)

    @property
    def label(self) -> str:
        """Call-log identity label (``caller:<id>`` / ``caller-pin:<id>`` /
        ``ephemeral`` / ``shared``)."""
        if self.ephemeral:
            return "ephemeral"
        if not self.id:
            return "shared"
        return f"caller-pin:{self.id}" if self.verified else f"caller:{self.id}"


def normalize_id(channel: str, raw: str | None) -> str:
    """The durable id for ``raw`` on ``channel``, or "" when withheld.

    Phone: separators are dropped, then the value must be an optional ``+``
    followed by digits. ``+30 210 123`` → ``+30210123``; ``abc+30210`` → "";
    ``anonymous`` → "". Other channels (none yet) use a conservative token
    rule so a future channel cannot slip path characters into a slug.
    """
    value = (raw or "").strip()
    if channel == PHONE:
        compact = _PHONE_SEPARATORS.sub("", value)
        if compact.lower() in _WITHHELD or not _PHONE_ID_RE.fullmatch(compact):
            return ""
        return compact
    if value.lower() in _WITHHELD or not re.fullmatch(r"[A-Za-z0-9._@+-]{1,64}", value):
        return ""
    return value


def slug_for(ident_id: str) -> str:
    """Path component for a normalised id (the phone ``+`` is dropped so
    ``+30210…`` and ``30210…`` are one caller)."""
    return ident_id.lstrip("+")


def _require_channel(channel: str) -> str:
    if not _CHANNEL_RE.fullmatch(channel or ""):
        raise ValueError(f"invalid external channel {channel!r}")
    return channel


def _require_session_uuid(session_id: str) -> str:
    """Ephemeral trees are keyed by the session id; a client-supplied id
    must be a UUID before it becomes a path component."""
    return str(uuid.UUID(str(session_id)))


def resolve(
    channel: str,
    raw_id: str | None,
    *,
    session_id: str,
    remember: bool = True,
    verified: bool = False,
    shared: bool = False,
    tree: bool = True,
) -> ExternalIdentity:
    """Resolve the external identity for one session.

    ``shared`` → the id-less external principal (no tree, no memory).
    ``tree=False`` → the caller keeps their id (prompt, call log) but gets no
    tree at all (an agent whose mode has no personal scope). Otherwise a
    durable id with ``remember`` → the caller's own tree; a withheld id, or
    ``remember=False`` → an ephemeral tree keyed by ``session_id``
    (validated as a UUID).
    """
    channel = _require_channel(channel)
    if shared:
        return ExternalIdentity(channel, "", "", False, bool(verified), f"{channel}:")
    ident = normalize_id(channel, raw_id)
    if not tree:
        return ExternalIdentity(
            channel, ident, "", False, bool(verified),
            f"{channel}:{ident}" if ident else f"{channel}:",
        )
    if not ident or not remember:
        sid = _require_session_uuid(session_id)
        return ExternalIdentity(
            channel, ident, f"{EPHEMERAL_DIRNAME}/{sid}", True, bool(verified),
            f"{channel}:{_EPHEMERAL_PREFIX}{sid}",
        )
    return ExternalIdentity(
        channel, ident, slug_for(ident), False, bool(verified), f"{channel}:{ident}",
    )


def external_home(agent_dir: Path, ident: ExternalIdentity) -> Path | None:
    """Host path of the caller's tree, or None when the session has none."""
    if not ident.has_tree:
        return None
    return Path(agent_dir) / EXTERNALS_DIRNAME / ident.channel / ident.slug


def tree_of(agent_dir: Path, channel: str, slug: str) -> Path:
    """Host path of an outside person's tree from their record's channel and
    slug. A slug that is not one path component is refused (ValueError)."""
    from auth.request_path import has_traversal
    if not slug or "/" in slug or "\\" in slug or has_traversal(slug):
        raise ValueError(f"not a tree name: {slug!r}")
    return Path(agent_dir) / EXTERNALS_DIRNAME / _require_channel(channel) / slug


def scratch_home(agent_dir: Path, channel: str, session_id: str) -> Path:
    """The home of an outside session whose identity has no tree (a
    Shared-only agent's caller, the route-less fallback): an ephemeral tree
    keyed by the session id, where the engine keeps its state and nothing
    durable lives, pruned at the session's end like a withheld number's."""
    channel = _require_channel(channel)
    try:
        sid = _require_session_uuid(session_id)
    except ValueError:
        # A session id that is no UUID (a builder's own) becomes a path
        # component through its digest, never as it came.
        import hashlib
        sid = hashlib.sha256(str(session_id).encode()).hexdigest()[:32]
    home = Path(agent_dir) / EXTERNALS_DIRNAME / channel / EPHEMERAL_DIRNAME / sid
    from services.infra import safe_fs
    safe_fs.mkdirs_beneath(*_beneath(home))
    return home


def _beneath(home: str | Path) -> tuple[Path, str]:
    """The agent's real folder and ``home``'s rel below it, from the path as
    written (``core/agent_folder``): an agent folder that is the operator's
    link is followed once, nothing below it ever is."""
    from core import agent_folder
    return agent_folder.beneath_agent(os.path.abspath(os.fspath(home)))


def _ephemeral_rel(rel: str) -> bool:
    """Whether an agent-relative ``rel`` names an ephemeral tree:
    ``externals/<channel>/_ephemeral/<name>``, nothing more or less."""
    parts = rel.split("/")
    return len(parts) == 4 and parts[0] == EXTERNALS_DIRNAME and parts[2] == EPHEMERAL_DIRNAME


def is_ephemeral_home(home: str | Path | None) -> bool:
    """Whether ``home`` is a tree ``prune_ephemeral`` removes: one that ends
    in ``externals/<channel>/_ephemeral/<name>``."""
    if not home:
        return False
    parts = Path(home).parts
    return len(parts) >= 4 and parts[-4] == EXTERNALS_DIRNAME and parts[-2] == EPHEMERAL_DIRNAME


def touch_home(home: str) -> None:
    """A caller tree's own time set to now (a call is activity, the retention
    sweep keys on the newest time). Opened beneath the agent's real folder
    with no link followed: a home that is missing or not a plain folder is
    left as it is."""
    from services.infra import safe_fs
    try:
        root, rel = _beneath(home)
        fd = safe_fs.open_beneath(root, rel, os.O_RDONLY)
    except OSError:
        return
    try:
        os.utime(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def parse_claim(claim: str) -> tuple[str, str, str]:
    """``(channel, id, ephemeral_session_id)`` for a JWT ``ext`` value.

    Raises ``ValueError`` on a malformed claim (the token was signed by the
    proxy, so a bad shape means a bug, not an attacker).
    """
    if not claim or ":" not in claim:
        raise ValueError(f"malformed external claim {claim!r}")
    channel, rest = claim.split(":", 1)
    _require_channel(channel)
    if rest.startswith(_EPHEMERAL_PREFIX):
        return channel, "", _require_session_uuid(rest[len(_EPHEMERAL_PREFIX):])
    if rest and normalize_id(channel, rest) != rest:
        raise ValueError(f"malformed external claim {claim!r}")
    return channel, rest, ""


def home_from_claim(agent_dir: Path, claim: str) -> Path | None:
    """The caller tree a signed ``ext`` claim points at (the deterministic
    inverse of :func:`resolve`), or None for an id-less claim."""
    channel, ident, ephemeral_sid = parse_claim(claim)
    base = Path(agent_dir) / EXTERNALS_DIRNAME / channel
    if ephemeral_sid:
        return base / EPHEMERAL_DIRNAME / ephemeral_sid
    if ident:
        return base / slug_for(ident)
    return None


def is_external_ctx(ctx) -> bool:
    """True when ``ctx`` (a SecurityContext, or anything shaped like one) is
    an external principal. Compares the ``principal`` field so a duck-typed
    or mocked context without the field is never mistaken for external."""
    return getattr(ctx, "principal", None) == "external"


def external_home_of(ctx) -> str:
    """``ctx.external_home`` as a plain string ("" when absent or not a str)."""
    home = getattr(ctx, "external_home", "")
    return home if isinstance(home, str) else ""


def caller_home_of(ctx) -> str:
    """The person's tree that ``/caller`` names in a session: an outside
    caller's own during a call, the tree of the person a wrap-up writes up
    after it ("" for a session with neither)."""
    home = external_home_of(ctx)
    if home or not getattr(ctx, "wrapup", ""):
        return home
    home = getattr(ctx, "wrapup_home", "")
    return home if isinstance(home, str) else ""


def prune_session_scratch(agent_dir: Path, session_id: str) -> None:
    """The scratch homes a session kept under the phone channel's
    ``_ephemeral/`` (a call's, a wrap-up's engine state), gone with it. The
    folder is listed beneath the agent's real folder: a link is never
    entered, and a missing or refused folder holds none."""
    if not session_id:
        return
    from services.infra import safe_fs
    folder = Path(agent_dir) / EXTERNALS_DIRNAME / PHONE / EPHEMERAL_DIRNAME
    try:
        root, rel = _beneath(folder)
        walk = safe_fs.walk_beneath(root, rel)
        try:
            names = [n for n in next(walk).dirs if n.startswith(session_id)]
        finally:
            walk.close()
    except FileNotFoundError:
        return
    except (OSError, StopIteration):
        logger.warning("Scratch homes of session %s not listed: %s", session_id[:8], folder, exc_info=True)
        return
    for name in names:
        prune_ephemeral(folder / name)


def prune_ephemeral(home: Path | None) -> bool:
    """Remove an ephemeral caller tree at teardown, beneath the agent's real
    folder with no link followed at any part. Refuses anything that is not
    ``externals/<channel>/_ephemeral/<name>`` (a durable tree is only ever
    removed by the retention sweep, with bookkeeping). Returns True when the
    tree was removed."""
    if home is None:
        return False
    from services.infra import safe_fs
    try:
        root, rel = _beneath(home)
    except OSError:
        return False
    if not _ephemeral_rel(rel):
        return False
    try:
        safe_fs.rmtree_beneath(root, rel)
    except FileNotFoundError:
        return False
    except OSError:
        logger.warning("Ephemeral caller tree not removed: %s", home, exc_info=True)
        return False
    return True
