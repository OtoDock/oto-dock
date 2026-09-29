"""The session kinds — who drives a session, and the questions the platform
asks about that (core-seams phase 4).

One axis, four carriers, every spelling frozen:

- ``client_type`` — the kind of a SESSION: ``AgentConfig.client_type`` at
  spawn, recorded on the in-memory session index by the layer
  (``session_state._record_session_use``), forwarded opaquely in the
  satellite start payload, read by the permission authority, the checks
  evaluator and the adapters registry (``proxy/adapters``, keyed by the
  kind's name). Written as ``dashboard`` / ``phone`` / ``task`` /
  ``meeting`` / ``app``; ``""`` is a session whose kind nobody recorded (a
  remote re-adoption after a proxy restart, the delivery ladder's one-shot
  resume) — never a kind. ``trigger`` and ``internal`` are declared and
  unwritten: a trigger-fired run is a ``task`` session.
- ``chats.source_type`` — the kind of a CHAT ROW: ``chat`` (the column
  default: the dashboard, the otodock CLI session, a chat-surface delegate
  worker), ``phone``, and — from this phase on — ``task`` on the rows the
  scheduler mints. Rows minted before the write carry ``chat`` with a
  ``task-`` id; :func:`of_chat` is the one reader that knows (no backfill).
- the pump's ``source_type`` — the kind of a TURN'S DRIVER, set by whoever
  builds the ``ChatStreamPump`` and persisted into ``usage_records``: the
  scheduler says ``task``, the phone socket ``phone``, the orchestrator
  ``meeting``, a dashboard turn ``chat``. It is the session's kind, never
  the row's (:func:`driver_source_type`).
- the ``task-{run_id}`` chat id — the scheduler's mint (:func:`task_chat_id`)
  and the durable marker of a task run's chat (:func:`is_task_chat_id`,
  :func:`run_id_of_chat`). ``meeting-<id>`` is a pump SESSION id, never a
  chat id; a task DEFINITION id (``task-<slug>-…``) never becomes one.

The kind's ``name`` is also the word the MCP manifests' ``exclude_from``
lists (``task`` / ``phone`` / ``meeting``; ``terminal`` and ``external``
are not kinds — a placement and a principal). ``chats.origin``
(``dashboard`` / ``otodock`` / ``delegated``) is a chat-list badge, not a
kind. The owner sentinels (``task::<agent>``, ``agent::<agent>``,
``phone``) live with the owners in ``core/session/visibility.py``.

Stdlib only — a leaf every side imports at module level.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SessionKind:
    """One kind: its two frozen spellings and the facts generic code asks."""

    name: str             # the client_type spelling; the manifests' exclude_from word
    source_type: str      # its spelling in the source_type vocabulary: the chat row it
                          # mints, the pump it drives, the usage rows it bills ("" = none)
    mints_chat: bool = False      # writes chat rows with that source_type (a meeting's
                                  # turns land in the PARENT chat: it mints none)
    attended: bool = False        # a person answers questions and approvals
    external_driven: bool = False  # an out-of-band driver owns the stream; the dashboard views read-only
    judged: bool = False          # the checks evaluator judges its turns


DASHBOARD = SessionKind("dashboard", "chat", mints_chat=True, attended=True, judged=True)
PHONE = SessionKind("phone", "phone", mints_chat=True, external_driven=True)
TASK = SessionKind("task", "task", mints_chat=True, judged=True)
MEETING = SessionKind("meeting", "meeting")
TRIGGER = SessionKind("trigger", "")
INTERNAL = SessionKind("internal", "")
APP = SessionKind("app", "")

KINDS: tuple[SessionKind, ...] = (DASHBOARD, PHONE, TASK, MEETING, TRIGGER, INTERNAL, APP)

_BY_NAME = {k.name: k for k in KINDS}
_BY_ROW_SOURCE_TYPE = {k.source_type: k for k in KINDS if k.mints_chat}

#: The chat ids the scheduler mints: ``task-{run_id}`` (``runner._execute_task``).
TASK_CHAT_ID_PREFIX = "task-"

#: The pump ``source_type`` spellings of the kinds an out-of-band driver owns
#: — the dashboard never attaches to their pumps, and the agent Conversations
#: tab lists exactly these rows.
EXTERNAL_DRIVEN_SOURCE_TYPES: frozenset[str] = frozenset(
    k.source_type for k in KINDS if k.external_driven)

#: The pump ``source_type`` spellings whose live turn takes a mid-turn steer:
#: a chat's own turn or a task lane. A meeting turn, a phone turn and a title
#: generation keep the post-turn queue — the dashboard's steer branch and the
#: server-side delivery rungs gate on the same set.
STEERABLE_SOURCE_TYPES: frozenset[str] = frozenset(
    k.source_type for k in (DASHBOARD, TASK))


def of(client_type: str | None) -> SessionKind | None:
    """The kind a ``client_type`` names, or None for ``""`` and a spelling no
    kind has — the platform does not invent a kind for a session nobody
    classified."""
    return _BY_NAME.get(client_type or "")


def attended(client_type: str | None) -> bool:
    """Whether a person is there to answer a question or an approval. A
    session whose kind was never recorded (``""``) counts as attended — the
    permission gate errs towards asking a person who may be there."""
    kind = of(client_type)
    return kind.attended if kind is not None else True


def judged(client_type: str | None) -> bool:
    """Whether the checks evaluator judges this session's turns (CHECKS.md):
    the dashboard's and the scheduler's; an unrecorded kind is judged."""
    kind = of(client_type)
    return kind.judged if kind is not None else True


def source_types(*kinds: SessionKind) -> list[str]:
    """The ``chats.source_type`` spellings of ``kinds`` — the resolved list a
    query takes as a parameter (a SQL literal cannot read a descriptor)."""
    return [k.source_type for k in kinds if k.source_type]


def task_chat_id(run_id: str) -> str:
    """The chat id of a task run — the one mint."""
    return f"{TASK_CHAT_ID_PREFIX}{run_id}"


def is_task_chat_id(chat_id: str | None) -> bool:
    """Whether a chat id has the scheduler's shape. The shape is the durable
    marker of a task run's chat: rows minted before the column was written
    carry ``chat`` in ``source_type`` and nothing else says task. A site
    that needs the RUN keeps asking the store for it — the shape alone
    grants nothing."""
    return bool(chat_id) and chat_id.startswith(TASK_CHAT_ID_PREFIX)


def run_id_of_chat(chat_id: str | None) -> str:
    """The run id a task chat id carries, ``""`` for any other id."""
    if not is_task_chat_id(chat_id):
        return ""
    return chat_id[len(TASK_CHAT_ID_PREFIX):]


def of_chat(row: dict | None) -> SessionKind:
    """The kind of a chat row — the read-side resolver.

    The column first: ``task`` → :data:`TASK`, ``phone`` → :data:`PHONE`
    (the spellings of the kinds that mint rows). ``chat``, ``""`` and a
    missing key (the column is NOT NULL DEFAULT 'chat', so only a dict
    built elsewhere lacks it) fall back to the id: a ``task-`` id is a task
    run's chat minted before the write (no backfill — those rows exist for
    as long as the install does), everything else is the dashboard's. A
    value that names no row kind resolves to :data:`DASHBOARD` (the
    default's kind): a read path never raises on data.
    """
    row = row or {}
    value = row.get("source_type") or ""
    if value and value != DASHBOARD.source_type:
        return _BY_ROW_SOURCE_TYPE.get(value, DASHBOARD)
    if is_task_chat_id(row.get("id") or ""):
        return TASK
    return DASHBOARD


def driver_source_type(client_type: str | None, row: dict | None) -> str:
    """The pump ``source_type`` for a turn the platform drives INTO a live
    session (a delegate-result wake): the SESSION's kind — the scheduler's
    own session says ``task``, a phone call ``phone``, a dashboard session
    ``chat``. A session whose kind was never recorded takes the row's kind
    only when that kind is externally driven (a phone chat's pump must stay
    one the dashboard never attaches to), else the dashboard's spelling —
    the driver is a person's session until something says otherwise."""
    kind = of(client_type)
    if kind is not None and kind.source_type:
        return kind.source_type
    row_kind = of_chat(row)
    if row_kind.external_driven:
        return row_kind.source_type
    return DASHBOARD.source_type
