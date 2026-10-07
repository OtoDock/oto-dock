"""The chat's input queue: the messages typed while its turn runs.

The queue belongs to the CHAT, not to the socket that typed into it: one
registry per chat, write-through to ``chat_input_queue`` (one row per
message while it waits, so a proxy restart loses nothing), loaded from the
table on the chat's first touch. A message leaves the queue when a turn
accepts it (``accept_job``: the row goes and the ``chat_messages`` row lands
in one lane job, ordered after every block the turn produced before it),
when its author cancels it, when the person's sign-in ends, or when the turn
it waited for ends other than cleanly or is stopped, in which case it is
RETURNED to its author's composer (``return_items``) rather than sent into
the same wall. Every queue frame reaches every socket of the chat's audience
and of the items' authors (``fan_out``), so a second tab or a teammate on a
shared chat sees the same chips.

At every pump end the registry delivers what still waits (``on_pump_end``):
an attached viewer drains its own messages on its loop's way out, and what
is left after a short grace, or at once with no viewer, goes out headless as
its author, through a connection object of that person (``deliver``): the
same code a socket runs for a turn, with the sender's gates. Every turn
starter that takes queued input holds the chat's claim (``try_claim``) until
its pump is registered, so two of them never start two turns.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from core.events import chat_writer
from core.events.common_events import TurnInput
from storage import database as task_store
from storage.pg import run_db
from ws import wire_events as wire

logger = logging.getLogger("claude-proxy")

#: Waiting messages a chat holds at most (a client cannot grow the table
#: without bound by spamming ``chat`` while a turn streams).
CAP = 64
#: Accepted ids a chat remembers, so a re-send after a reconnect of a
#: message the turn already took is acked, not queued twice.
ACCEPTED_MEMORY = 64
#: How long after a pump's end an attached viewer gets to drain its own
#: messages before the headless delivery takes what is left.
VIEWER_GRACE_S = 2.0

QUEUED = "queued"
DUPLICATE = "duplicate"
ACCEPTED = "accepted"
FULL = "full"

#: What a delivery's driver answers (``_deliver_queued``): the turn went
#: out, the author may not run it (the messages go back with the card), or
#: the turn could not start (the messages wait for the next end or touch).
DELIVERY_SENT = "sent"
DELIVERY_GATED = "gated"
DELIVERY_DEFERRED = "deferred"

#: A message waits for its chat's machine to reconnect (the ``queued`` chip's
#: ``waiting``, FROZEN wire): held in memory on the item, cleared when the
#: machine is back.
WAITING_RECONNECT = "reconnect"

#: The return reasons the server decides: the author also gets the card,
#: since the live frame that restores the composer can be lost.
_CARD_REASONS = {
    "turn_failed": wire.UNDELIVERED_TURN_FAILED,
    "drive_refused": wire.UNDELIVERED_QUEUED,
}


@dataclass(eq=False)
class QueuedInput:
    """One waiting message: who typed it, what the engine and the row each
    get (``TurnInput``), the connection it came from (its id, and in this
    process its object, which a headless delivery may drive as that
    person), its place in the queue's sequence, and the chat its author was
    viewing when that is not the queue's own (a task chat viewed through a
    sibling run's pump)."""

    queue_id: str
    chat_id: str
    author_sub: str
    item: TurnInput
    origin_conn: str = ""
    seq: int = 0
    view_chat_id: str = ""
    conn: Any = field(default=None, repr=False)
    waiting: str = ""

    def chip(self) -> dict:
        """The keys a ``queued`` frame and a ``queue_snapshot`` entry carry."""
        out = {"queue_id": self.queue_id, "text": self.item.text,
               "author_sub": self.author_sub, **self.item.frame_fields()}
        if self.view_chat_id:
            out["view_chat_id"] = self.view_chat_id
        if self.waiting:
            out["waiting"] = self.waiting
        return out


def _row_to_input(row: dict) -> QueuedInput:
    meta = json.loads(row.get("event_data") or "{}") if row.get("event_data") else {}
    images = json.loads(row.get("images") or "[]") if row.get("images") else []
    return QueuedInput(
        queue_id=row["queue_id"], chat_id=row["chat_id"],
        author_sub=row.get("author_sub") or "",
        item=TurnInput(text=row.get("text") or "", cli_text=row.get("cli_text") or "",
                       images=images, image_meta=list(meta.get("images") or []),
                       files=list(meta.get("files") or [])),
        origin_conn=row.get("origin_conn") or "",
    )


def combined_fields(items: list[QueuedInput]) -> dict:
    """The text and the attachments of several messages as one composer
    restore or one drained bubble: texts joined with a blank line, the
    attachment meta concatenated."""
    batch = TurnInput.combine([qi.item for qi in items])
    return {"text": batch.text, **batch.frame_fields()}


@dataclass
class ChatInputQueue:
    chat_id: str
    items: list[QueuedInput] = field(default_factory=list)
    accepted: deque = field(default_factory=lambda: deque(maxlen=ACCEPTED_MEMORY))
    owner_sub: str = ""
    agent: str = ""
    # A turn starter that takes queued input holds it from its check of the
    # chat until its pump is registered (``try_claim`` / ``release``): the
    # holder's token, so only that holder's release frees it.
    _claim: object | None = None
    _seq: int = 0
    _loaded: bool = False
    _loading: "asyncio.Future | None" = None

    async def load(self) -> bool:
        """The rows and the chat's owner and agent, once per process (a
        concurrent first touch awaits the same read). True when this call
        restored waiting rows from the table."""
        if self._loaded:
            return False
        if self._loading is not None:
            await asyncio.shield(self._loading)
            return False
        loop = asyncio.get_running_loop()
        self._loading = loop.create_future()
        restored: list[QueuedInput] = []
        try:
            cid = self.chat_id
            rows, chat = await run_db(lambda: (task_store.list_chat_input_queue(cid),
                                               task_store.get_chat(cid)))
            known = {qi.queue_id for qi in self.items}
            restored = [_row_to_input(r) for r in rows if r["queue_id"] not in known]
            for qi in restored:
                qi.seq = self._next_seq()
            # The rows waited before anything added in this process.
            self.items[:0] = restored
            if chat:
                self.owner_sub = chat.get("user_sub") or ""
                self.agent = chat.get("agent") or ""
            self._loaded = True
            if restored:
                logger.info(f"chat={cid[:8]}: {len(restored)} queued message(s) loaded "
                            f"from the table")
        finally:
            fut, self._loading = self._loading, None
            if not fut.done():
                fut.set_result(None)
        return bool(restored)

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def seq(self) -> int:
        """The sequence number of the newest item ever queued here: a return
        bounded by it leaves every later item for the next turn."""
        return self._seq

    def find(self, queue_id: str) -> QueuedInput | None:
        return next((qi for qi in self.items if qi.queue_id == queue_id), None)

    def index_of(self, queue_id: str) -> int:
        return next((i for i, qi in enumerate(self.items) if qi.queue_id == queue_id), -1)

    def of(self, author_sub: str) -> list[QueuedInput]:
        return [qi for qi in self.items if qi.author_sub == author_sub]

    def snapshot(self) -> list[dict]:
        return [qi.chip() for qi in self.items]

    def audience(self) -> list[str]:
        """The subs whose sockets see this chat's queue: the owner, or every
        user of the agent for a chat that runs as it."""
        from services.notifications.notification_manager import chat_status_targets
        return chat_status_targets(self.owner_sub, self.agent)

    @property
    def claimed(self) -> bool:
        return self._claim is not None

    def try_claim(self) -> object | None:
        """The chat for one turn starter: its token when no pump drives the
        chat and no other starter holds it, else None. A check and a set
        with no await between."""
        from core.events.stream_pump import _active_pumps
        live = _active_pumps.get(self.chat_id)
        # A failed pump keeps the chat until it leaves the registry: its end
        # returns the messages queued before the failure first, so a starter
        # meanwhile cannot take them as a delivery.
        if self._claim is not None or (live is not None and (
                not live.is_done or getattr(live, "_return_upto", None) is not None)):
            return None
        self._claim = object()
        return self._claim

    def release(self, token: object | None, *, rearm: bool = True) -> None:
        """Free the chat if ``token`` still holds it (a second release, or a
        stale holder's, changes nothing). Messages still waiting with no
        pump to drain them go out as the next turn (the holder's start
        failed, or it held the chat for someone else's messages), unless
        ``rearm`` is off: a delivery whose turn could not start would arm
        itself again at once."""
        if token is None or self._claim is not token:
            return
        self._claim = None
        if not rearm:
            return
        from core.events.stream_pump import _active_pumps
        live = _active_pumps.get(self.chat_id)
        if self.items and (live is None or live.is_done):
            arm(self.chat_id)

    async def add(self, queue_id: str, author_sub: str, item: TurnInput, *,
                  origin_conn: str = "", conn: Any = None, view_chat_id: str = "",
                  waiting: str = "",
                  ) -> tuple[str, QueuedInput | None, int]:
        """Queue one message: ``(QUEUED, the entry, its index)``, or
        ``DUPLICATE`` with the entry still waiting under that id for the same
        author, ``ACCEPTED`` for an id a turn already took, ``FULL`` at the
        cap. An id another person already holds is re-minted."""
        await self.load()
        found = self.find(queue_id)
        if found is not None:
            if found.author_sub == author_sub:
                return DUPLICATE, found, self.index_of(queue_id)
            queue_id = mint_queue_id()
        elif queue_id in self.accepted:
            return ACCEPTED, None, -1
        if len(self.items) >= CAP:
            return FULL, None, -1
        qi = QueuedInput(queue_id=queue_id, chat_id=self.chat_id, author_sub=author_sub,
                         item=item, origin_conn=origin_conn, seq=self._next_seq(),
                         view_chat_id=view_chat_id, conn=conn, waiting=waiting)
        self.items.append(qi)
        meta = item.event_meta
        await chat_writer.submit(
            self.chat_id,
            functools.partial(
                task_store.enqueue_chat_input, self.chat_id, queue_id, author_sub,
                text=item.text, cli_text=item.cli_text,
                event_data=json.dumps(meta) if meta else "",
                images=json.dumps(item.images) if item.images else "",
                origin_conn=origin_conn,
            ),
            label="queue_add",
        )
        return QUEUED, qi, self.index_of(queue_id)

    async def remove(self, qi: QueuedInput) -> int:
        """Take one waiting message out (a cancel): the index it had, -1
        when it is no longer waiting."""
        index = self.index_of(qi.queue_id)
        if index < 0 or self.items[index] is not qi:
            return -1
        self.items.pop(index)
        await self.drop_job([qi])
        return index

    def take(self, author_sub: str | None = None, *, upto_seq: int | None = None,
             ) -> list[QueuedInput]:
        """Take the waiting messages of ``author_sub`` (every message for
        None), up to ``upto_seq`` when given, out of the queue, in queue
        order, for a turn or a return: the rows stay until ``accept_job``
        turns them into user rows or ``drop_job`` deletes them, and
        ``put_back`` returns entries a taker could not use."""
        taken = [qi for qi in self.items
                 if (author_sub is None or qi.author_sub == author_sub)
                 and (upto_seq is None or qi.seq <= upto_seq)]
        if taken:
            gone = {id(qi) for qi in taken}
            self.items[:] = [qi for qi in self.items if id(qi) not in gone]
        return taken

    def put_back(self, taken: list[QueuedInput]) -> None:
        """Entries a taker could not send go back ahead of the rest."""
        self.items[:0] = taken
        self.items.sort(key=lambda qi: qi.seq)

    def accept_job(self, taken: list[QueuedInput], *, extra_meta: dict | None = None,
                   ) -> asyncio.Future:
        """The turn took ``taken``: their queue rows go and one user message
        per entry lands, in one lane job submitted NOW (the caller orders it
        by submitting it before anything else of the turn). The future
        resolves to the message ids. An entry with no queue id (a voice
        utterance, a nudge) is a row only."""
        rows = []
        for qi in taken:
            meta = dict(qi.item.event_meta)
            if extra_meta:
                meta.update(extra_meta)
            rows.append({"queue_id": qi.queue_id, "text": qi.item.text,
                         "event_data": json.dumps(meta) if meta else "",
                         "author_sub": qi.author_sub})
            if qi.queue_id:
                self.accepted.append(qi.queue_id)
        return chat_writer.submit(
            self.chat_id,
            functools.partial(task_store.accept_chat_inputs, self.chat_id, rows),
            label="queue_accept",
        )

    def drop_job(self, taken: list[QueuedInput]) -> asyncio.Future:
        """The rows of entries that leave without a turn (a cancel, a
        return, a refusal, a revocation)."""
        ids = [qi.queue_id for qi in taken if qi.queue_id]
        cid = self.chat_id

        def _job() -> None:
            for qid in ids:
                task_store.delete_chat_input(cid, qid)
        return chat_writer.submit(cid, _job, label="queue_drop")


_registry: dict[str, ChatInputQueue] = {}
_tasks: set[asyncio.Task] = set()


def _frozen() -> bool:
    """The proxy is shutting down (the scheduler's flag, raised before any
    session closes): no return and no delivery, what waits stays in the
    table for the next process."""
    from services.scheduler import shared
    return bool(shared._shutting_down)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def get(chat_id: str) -> ChatInputQueue:
    """The chat's queue object (unloaded until ``loaded`` or a write)."""
    q = _registry.get(chat_id)
    if q is None:
        q = ChatInputQueue(chat_id=chat_id)
        _registry[chat_id] = q
    return q


async def loaded(chat_id: str) -> ChatInputQueue:
    """The chat's queue, loaded. Rows restored from the table on the first
    touch (messages queued before a restart) arm the delivery for their
    authors who have a live connection."""
    q = get(chat_id)
    if await q.load():
        arm(chat_id)
    return q


def peek(chat_id: str) -> list[QueuedInput]:
    """The waiting messages known in this process, without a read."""
    q = _registry.get(chat_id)
    return list(q.items) if q is not None else []


def busy(chat_id: str) -> bool:
    """Something of the chat's queue is still to go out (lane quiescence)."""
    q = _registry.get(chat_id)
    return q is not None and (bool(q.items) or q.claimed)


def mint_queue_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# The frames
# ---------------------------------------------------------------------------

def fan_out(chat_id: str, frame: dict, *, authors=(), returned_to: dict[str, dict] | None = None,
            ) -> set[str]:
    """Push ``frame`` to every socket of the chat's audience and of
    ``authors`` through the live queue (drained during a turn and between
    turns alike). ``returned_to`` maps an author's sub to the composer fields
    their sockets get on top (``returned: true``, the text and the
    attachments). Returns the subs in ``returned_to`` that had no socket to
    take it."""
    from services.notifications.notification_manager import push_live
    q = get(chat_id)
    base = {**frame, "chat_id": chat_id}
    unreached: set[str] = set()
    targets = set(q.audience()) | {a for a in authors if a} | set(returned_to or {})
    for sub in targets:
        copy = dict(base)
        extra = (returned_to or {}).get(sub)
        if extra is not None:
            copy.update(extra)
            copy["returned"] = True
        if push_live(sub, copy) == 0 and extra is not None:
            unreached.add(sub)
    return unreached


def steered_frame(chat_id: str, qi: QueuedInput, ids: list[int],
                  extra: dict | None = None) -> dict:
    """The ``steered`` frame of a message the running turn took: its row's
    id (its place in the chat), its queue id, its text and attachments."""
    return {"type": wire.STEERED, "chat_id": chat_id, "queue_id": qi.queue_id,
            "message_id": ids[0] if ids else None, "text": qi.item.text,
            **qi.item.frame_fields(), **(extra or {})}


async def record_steer_late(chat_id: str, qi: QueuedInput, *,
                            extra_meta: dict | None = None,
                            frame_extra: dict | None = None) -> None:
    """A steer the engine accepted on a pump already done (its final save
    is queued on the lane, so the row lands after it): the row, then the
    frame to the chat's audience and the author."""
    q = await loaded(chat_id)
    ids = await q.accept_job([qi], extra_meta=extra_meta)
    fan_out(chat_id, steered_frame(chat_id, qi, ids, frame_extra), authors=(qi.author_sub,))


def fan_out_snapshot(chat_id: str) -> None:
    """The chat's whole queue to its audience (each client replaces its
    chips with it)."""
    q = get(chat_id)
    fan_out(chat_id, {"type": wire.QUEUE_SNAPSHOT, "messages": q.snapshot()},
            authors=tuple({qi.author_sub for qi in q.items}))


def clear_waiting(chat_id: str) -> None:
    """The chat's machine is back: no chip keeps saying it is reconnecting.
    The mark lives in memory only, so a queue not loaded here has none."""
    marked = [qi for qi in peek(chat_id) if qi.waiting]
    for qi in marked:
        qi.waiting = ""
    if marked:
        fan_out_snapshot(chat_id)


def announce_queued(qi: QueuedInput, index: int) -> None:
    """The ``queued`` chip to the chat's audience and its author."""
    fan_out(qi.chat_id, {"type": wire.QUEUED, "index": index, **qi.chip()},
            authors=(qi.author_sub,))


async def return_items(chat_id: str, taken: list[QueuedInput], reason: str) -> None:
    """``taken`` leave without a turn and go back to their authors:
    ``queue_cleared`` with the reason to the chat's audience, the composer
    fields to each author's sockets, and the undelivered card for a reason
    the server decided (always: the live frame can be lost) or for a Stop's
    return an author has no socket to take."""
    if not taken:
        return
    q = get(chat_id)
    await q.drop_job(taken)
    by_author: dict[str, list[QueuedInput]] = {}
    for qi in taken:
        by_author.setdefault(qi.author_sub, []).append(qi)
    returned = {sub: combined_fields(items) for sub, items in by_author.items() if sub}
    unreached = fan_out(chat_id, {"type": wire.QUEUE_CLEARED,
                                  "queue_ids": [qi.queue_id for qi in taken],
                                  "reason": reason},
                        authors=by_author, returned_to=returned)
    from core.events.stream_pump import shelve_undelivered
    card = _CARD_REASONS.get(reason)
    for sub, items in by_author.items():
        if card:
            shelve_undelivered(chat_id, [qi.item for qi in items], reason=card)
        elif reason in ("stopped", "revoked") and (not sub or sub in unreached):
            shelve_undelivered(chat_id, [qi.item for qi in items],
                               reason=wire.UNDELIVERED_STOPPED)
    logger.info(f"chat={chat_id[:8]}: {len(taken)} queued message(s) returned ({reason})")


async def return_all(chat_id: str, reason: str, *, upto_seq: int | None = None,
                     ) -> list[QueuedInput]:
    """Return every waiting message (up to ``upto_seq`` when given)."""
    q = await loaded(chat_id)
    taken = q.take(None, upto_seq=upto_seq)
    await return_items(chat_id, taken, reason)
    return taken


def return_soon(chat_id: str, reason: str) -> None:
    """``return_all`` from synchronous code (a task of its own)."""
    _spawn(return_all(chat_id, reason))


async def cancel_own(chat_id: str, author_sub: str) -> list[QueuedInput]:
    """The person's own waiting messages, cancelled (Edit): their composer
    gets them back, nobody gets a card."""
    q = await loaded(chat_id)
    taken = q.take(author_sub)
    await return_items(chat_id, taken, "cancel")
    return taken


async def drop_where(*, conn_id: str = "", author_sub: str = "", chat_id: str = "") -> None:
    """Messages that may no longer go out (the connection that queued them
    was closed for its session, or the person lost the chat): gone from the
    table and the registry, the chips cleared, nothing returned."""
    if not (conn_id or author_sub):
        return
    rows = await run_db(functools.partial(task_store.delete_chat_inputs_where,
                                          chat_id=chat_id, author_sub=author_sub,
                                          origin_conn=conn_id))
    by_chat: dict[str, list[str]] = {}
    for r in rows:
        by_chat.setdefault(r["chat_id"], []).append(r["queue_id"])
    for q in list(_registry.values()):
        if chat_id and q.chat_id != chat_id:
            continue
        gone = [qi for qi in q.items
                if (not conn_id or qi.origin_conn == conn_id)
                and (not author_sub or qi.author_sub == author_sub)]
        if gone:
            ids = {id(qi) for qi in gone}
            q.items[:] = [qi for qi in q.items if id(qi) not in ids]
            known = by_chat.setdefault(q.chat_id, [])
            known.extend(qi.queue_id for qi in gone if qi.queue_id not in known)
    for cid, ids in by_chat.items():
        await loaded(cid)   # the chat's audience
        fan_out(cid, {"type": wire.QUEUE_CLEARED, "queue_ids": ids, "reason": "revoked"},
                authors=(author_sub,))


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def on_pump_end(chat_id: str, *, return_upto: int | None, viewers: bool) -> None:
    """A chat's active pump ended. When it ended other than cleanly, what
    was queued up to ``return_upto`` goes back to its authors; what is left
    (a message sent after the error or the Stop) is delivered as the next
    turn: after an attached viewer's own drain (``VIEWER_GRACE_S``), or at
    once with no viewer."""
    if _frozen():
        return
    _spawn(_after_pump_end(chat_id, return_upto=return_upto, viewers=viewers))


async def _after_pump_end(chat_id: str, *, return_upto: int | None, viewers: bool) -> None:
    try:
        q = await loaded(chat_id)
        if return_upto is not None:
            await return_items(chat_id, q.take(None, upto_seq=return_upto), "turn_failed")
        if not q.items:
            return
        if viewers:
            await asyncio.sleep(VIEWER_GRACE_S)
        await deliver(chat_id)
    except Exception:
        logger.exception(f"chat={chat_id[:8]}: the queue's delivery after the turn failed")


def arm(chat_id: str) -> None:
    """Deliver what the chat holds once no pump drives it (a message queued
    with no pump left to drain it, rows restored after a restart)."""
    if _frozen():
        return

    async def _later() -> None:
        try:
            await asyncio.sleep(VIEWER_GRACE_S)
            await deliver(chat_id)
        except Exception:
            logger.exception(f"chat={chat_id[:8]}: the queue's delivery failed")
    _spawn(_later())


def _driver_for(author_sub: str, items: list[QueuedInput]):
    """A connection object of ``author_sub`` to run their messages' turn
    as them: a live one (the connection that queued the first message
    first), else the object of the connection that queued it (closed, as a
    closed socket runs its kicked first turn headless), else None."""
    if not author_sub:
        return None
    from ws.dashboard import _connections_by_user
    origin = items[0].origin_conn if items else ""
    live = [c for c in _connections_by_user.get(author_sub, ())
            if not c._ended.is_set() and not c._closed_by_revalidation]
    for c in live:
        if c.notify_connection_id == origin:
            return c
    if live:
        return live[0]
    for qi in items:
        c = qi.conn
        if c is not None and not getattr(c, "_closed_by_revalidation", True):
            return c
    return None


async def deliver(chat_id: str) -> bool:
    """Send the chat's waiting messages as its next turn, as their author,
    with no socket driving it: the oldest message's author first, every one
    of their messages as one combined turn, through a connection object of
    that person (``_deliver_queued``: the sender's gates, the heal, the
    dashboard producer that drains the rest at its end). An author with no
    connection object waits for their next touch. True when a turn went
    out."""
    from core.events.stream_pump import _active_pumps
    if _frozen():
        return False
    q = await loaded(chat_id)
    for author in list(dict.fromkeys(qi.author_sub for qi in q.items)):
        live = _active_pumps.get(chat_id)
        if live is not None and not live.is_done:
            return False   # re-armed at that pump's end
        mine = q.of(author)
        if not mine:
            continue
        conn = _driver_for(author, mine)
        if conn is None:
            continue
        claim = q.try_claim()
        if claim is None:
            return False   # the holder's release re-arms what is left
        taken = q.take(author)
        outcome, why = DELIVERY_DEFERRED, ""
        try:
            outcome, why = await conn._deliver_queued(chat_id, taken)
        except Exception:
            logger.exception(f"chat={chat_id[:8]}: the queued message's turn failed to start")
        finally:
            if outcome == DELIVERY_DEFERRED:
                q.put_back(taken)   # a cancellation mid-start included
                if any(qi.waiting for qi in taken):
                    fan_out_snapshot(chat_id)   # the chips say why they wait
            # A turn that could not start waits for the next pump end or
            # touch: its own release must not arm the same delivery again
            # (with another author waiting it would retry every 2 s).
            q.release(claim, rearm=outcome != DELIVERY_DEFERRED)
        if outcome == DELIVERY_SENT:
            return True
        if outcome == DELIVERY_GATED:
            logger.info(f"chat={chat_id[:8]}: queued message(s) not sent: {why}")
            await return_items(chat_id, taken, "drive_refused")
            continue
        logger.info(f"chat={chat_id[:8]}: queued message(s) wait for the next turn "
                    f"({why or 'the turn could not start'})")
        return False
    return False
