"""The prompts' answers: a permission, a Codex question, a location and a
plan review answered on this socket, each resolving the wait the engine
holds and told to the chat's other viewers (``permission_resolved``). The
handlers are the ``_on_<name>`` methods ``ws.wire_events.INBOUND`` names
for those four messages, called the way ``ws/dashboard_dispatch.py``
describes.

PromptController is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
"""

import functools
import logging
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, cast

from core.events import chat_writer
from core.events.common_events import TurnInput
from core.events.stream_pump import _active_pumps, _pending_permissions
from core.execution_layer import ASK_MODE, AUTO_REVIEW_MODE
from core.session import interactive_session
from core.session.session_state import (
    broadcast_chat_frame,
    pre_plan_mode,
    prompt_answered,
    prompt_waits,
    resolve_location,
    resolve_permission,
    resolve_question,
    set_session_mode,
)
from storage import database as task_store
from ws import wire_events as wire
from ws.dashboard_chat_support import mode_frame
from ws.dashboard_dispatch import StreamCtx

if TYPE_CHECKING:
    from ws.dashboard_host import DashboardHost as _Host
else:
    _Host = object

logger = logging.getLogger("otodock")

#: A plan review's implement actions and the mode each moves the chat to
#: ("Start in Auto", "Start in Ask").
_IMPLEMENT_MODES = {"implement_auto": AUTO_REVIEW_MODE, "implement_ask": ASK_MODE}
#: The action names before 1.9 (an older page's answer, a stored card).
PLAN_ACTIONS_BEFORE_1_9 = {"implement_accept_edits": "implement_auto",
                           "implement_default": "implement_ask"}
IMPLEMENT_PROMPT = "Please implement the plan now."

# The held questions and plan reviews answered lately, by request id: a
# second socket's late answer for one of them (its waiter gone, the id
# unknown again) is dropped, never replayed as a chat turn, never a second
# mode flip or implement turn.
_recently_answered: OrderedDict[str, float] = OrderedDict()
_RECENTLY_ANSWERED_CAP = 64
_RECENTLY_ANSWERED_S = 300.0


def _note_answered(request_id: str) -> None:
    now = time.monotonic()
    _recently_answered[request_id] = now
    _recently_answered.move_to_end(request_id)
    while len(_recently_answered) > _RECENTLY_ANSWERED_CAP:
        _recently_answered.popitem(last=False)


def _answered_lately(request_id: str) -> bool:
    at = _recently_answered.get(request_id)
    return at is not None and time.monotonic() - at < _RECENTLY_ANSWERED_S


class PromptController(_Host):
    """The four prompt answers and the one-prompt slot they advance."""

    # The connection state this mixin assigns, declared where it assigns it:
    # the type checker reads an attribute a mixin assigns from the mixin
    # first. Annotations only (strings), no class attributes.
    implementing_plan: "str"

    @staticmethod
    async def _advance_prompt_slot(pump, request_id: str, resolved: bool) -> None:
        """The pump's one-prompt slot moves on for an answer that resolved a
        wait or that names the prompt holding the slot. An answer to a card
        already retired (its wait gone) leaves the retried call's prompt in
        the slot, so a reconnect still re-presents it."""
        active = pump._permission_active
        if resolved or (active is not None and active.get("request_id") == request_id):
            await pump.resolve_active_permission()

    def _tell_card_resolved(self, chat_id: str, pump, frame: dict) -> None:
        """A card this socket answered, closed on every other viewer of the
        chat: through the chat's pump to the viewers attached mid-turn
        (``stream``'s pump, else the chat's active one), or, when no viewer
        is attached to a pump, through the chat-scoped broadcast to a socket
        viewing the chat between turns (an engine-prompt card shown with no
        pump). The answering socket gets it too; the page's second changes
        nothing."""
        live = pump if pump is not None else _active_pumps.get(chat_id)
        if live is not None and not live.is_done and live.push_ws_event(frame):
            return
        if chat_id:
            broadcast_chat_frame(chat_id, frame)

    @staticmethod
    def _forget_pending(request_id: str) -> None:
        for sid, pd in list(_pending_permissions.items()):
            if pd.get("request_id") == request_id:
                del _pending_permissions[sid]
                break

    async def _on_permission_response(self, msg: dict, *, stream: StreamCtx | None = None):
        # Resolve hook-based permission (unblocks the long-poll in hook endpoint).
        rid = msg["request_id"]
        approved = bool(msg.get("approved", True))
        if stream is not None:
            if await self._may_resolve_permission(rid):
                resolved = resolve_permission(rid, approved)
                self._forget_pending(rid)
                if resolved:
                    self._tell_card_resolved(stream.pump.chat_id, stream.pump, {
                        "type": wire.PERMISSION_RESOLVED, "request_id": rid,
                        "approved": approved, "chat_id": stream.pump.chat_id})
                await self._advance_prompt_slot(stream.pump, rid, resolved)
            return None
        # dual-control: while a local `otodock` terminal is the active
        # controller, the human answers permissions in the native TUI — drop a
        # (stale) dashboard response so it can't advance the agent out from
        # under them.
        _ds_isess = interactive_session.get(self.session_id) if self.session_id else None
        if _ds_isess is not None and _ds_isess.otodock_attached:
            pass
        elif await self._may_resolve_permission(rid):
            if resolve_permission(rid, approved) and self.chat_id:
                self._tell_card_resolved(self.chat_id, None, {
                    "type": wire.PERMISSION_RESOLVED, "request_id": rid,
                    "approved": approved, "chat_id": self.chat_id})
        else:
            return None
        self._forget_pending(rid)
        return None

    async def _on_question_response(self, msg: dict, *, stream: StreamCtx | None = None):
        rid = msg["request_id"]
        if stream is not None:
            # Codex request_user_input answer — the held turn resumes.
            # Validate the answerer drives this session, resolve the
            # waiter with the answers map, then ADVANCE the pump's
            # permission slot (else a later prompt in the same held turn
            # buffers forever + a reconnect re-renders the answered card).
            if await self._may_resolve_permission(rid):
                resolved = resolve_question(rid, msg.get("answers") or {})
                self._forget_pending(rid)
                if resolved:
                    _note_answered(rid)
                    self._tell_card_resolved(stream.pump.chat_id, stream.pump, {
                        "type": wire.PERMISSION_RESOLVED, "request_id": rid,
                        "chat_id": stream.pump.chat_id})
                await self._advance_prompt_slot(stream.pump, rid, resolved)
            return None
        # Codex request_user_input answer arriving between turns (safety net;
        # a held question normally resolves mid-stream). An answer for a
        # question another socket answered lately is dropped, never replayed.
        if await self._may_resolve_permission(rid):
            delivered = resolve_question(rid, msg.get("answers") or {})
            self._forget_pending(rid)
            if delivered:
                _note_answered(rid)
                if self.chat_id:
                    self._tell_card_resolved(self.chat_id, None, {
                        "type": wire.PERMISSION_RESOLVED, "request_id": rid,
                        "chat_id": self.chat_id})
            elif _answered_lately(rid):
                logger.info("WS dashboard question_response: request %s was answered "
                            "on another socket, dropped", rid[:8])
            else:
                await self._question_answer_fallback(msg)
        return None

    async def _on_location_response(self, msg: dict, *, stream: StreamCtx | None = None):
        # Only a connection that views a session and may drive its chat
        # answers for it (a read-only viewer of a shared chat never does).
        if not self.session_id or self._view_only or await self._deny_task_continue(
                self.chat_id, quiet=True):
            logger.info("WS dashboard: location answer dropped (not the driving connection)")
            return None
        if not resolve_location(msg["request_id"], {
            "lat": msg.get("lat"),
            "lng": msg.get("lng"),
            "accuracy": msg.get("accuracy"),
            "error": msg.get("error"),
        }, session_id=self.session_id):
            logger.info("WS dashboard: location answer dropped (not the asked session)")
        return None

    async def _on_plan_review_response(self, msg: dict, *, stream: StreamCtx | None = None):
        if stream is None:
            # Plan review between turns — can happen normally or after session death
            # dual-control: ignore a dashboard plan-review response while a local
            # `otodock` terminal controls the session (it reviews in the native TUI).
            _ds_isess = interactive_session.get(self.session_id) if self.session_id else None
            if _ds_isess is not None and _ds_isess.otodock_attached:
                return None
        rid = msg["request_id"]
        if not await self._may_resolve_permission(rid):
            return None
        # A card another socket answered, in the tick before its waiter
        # left or lately (the waiter gone, its id unknown again): nothing
        # more changes (no mode flip, no row, no implement turn).
        if prompt_answered(rid) or _answered_lately(rid):
            return None
        if stream is None:
            # Claimed before anything is awaited: a second tab's answer to a
            # card nobody waits on finds it answered (one implement turn).
            _note_answered(rid)
        action = PLAN_ACTIONS_BEFORE_1_9.get(msg.get("action", ""), msg.get("action", ""))
        plan_fn = msg.get("filename", "")
        implement_mode = _IMPLEMENT_MODES.get(action, "")
        # Approve ExitPlanMode for implement AND reject (cancel).
        # For "edit", deny so Claude stays in plan mode for revisions.
        approved = action != "edit"
        # Mid-turn only an answer a waiter takes, or one for the card the
        # pump holds (a retried call's prompt, its wait gone), acts on the
        # session; between turns a card nobody waits on (a Codex plan, a
        # stale plan of a dead session) acts while its plan row is pending:
        # the row is what a reload, a second tab and a restart all read.
        active = stream.pump._permission_active if stream is not None else None
        acts = (stream is None or prompt_waits(rid)
                or (active is not None and active.get("request_id") == rid))
        if stream is None and plan_fn and self.chat_id:
            if not await self._plan_row_pending(self.chat_id, plan_fn):
                return None
        # Set session mode BEFORE resolve_permission so the hook
        # endpoint sees the correct mode when it wakes up (prevents
        # race where hook checks stale "plan" mode).
        if acts and approved and self.session_id:
            if action == "reject":
                set_session_mode(self.session_id, pre_plan_mode(self.session_id))
            elif implement_mode:
                set_session_mode(self.session_id, implement_mode)
        resolved = resolve_permission(rid, approved)
        self._forget_pending(rid)
        if resolved or stream is None:
            _note_answered(rid)
            chat_for_card = stream.pump.chat_id if stream is not None else (self.chat_id or "")
            self._tell_card_resolved(chat_for_card, stream.pump if stream is not None else None, {
                "type": wire.PERMISSION_RESOLVED, "request_id": rid, "action": action,
                "chat_id": chat_for_card})
        if stream is not None:
            pump = stream.pump
            await self._advance_prompt_slot(pump, rid, resolved)
            if not acts:
                return None
            # Save the user's action in the DB turn block
            req_id = msg.get("request_id", "")
            for tb in pump._turn_blocks:
                if tb.get("type") == wire.PLAN_REVIEW and tb.get("request_id") == req_id:
                    tb["action"] = action
                    break
            # Mode/plan-status writes ride the chat lane so they
            # stay ordered with the pump's own plan-mode write.
            if self.chat_id and plan_fn and action == "reject":
                chat_writer.submit(
                    self.chat_id,
                    functools.partial(task_store.update_chat_plan_status,
                                      self.chat_id, plan_fn, "rejected"),
                    label="plan_rejected",
                )
                restored_mode = pre_plan_mode(self.session_id or "")
                chat_writer.submit(
                    self.chat_id,
                    functools.partial(task_store.update_chat, self.chat_id,
                                      permission_mode=restored_mode),
                    label="plan_mode_restore",
                )
                await self._send(mode_frame(restored_mode, self.chat_id))
            if implement_mode:
                # The engine's own mode follows (the CLI's word for it) before
                # the implement turn: queued, flushed between turns.
                self.pending_control_requests.append(("set_permission_mode", {"mode": implement_mode}))
                chat_writer.submit(
                    cast("str", self.chat_id),
                    functools.partial(task_store.update_chat, cast("str", self.chat_id),
                                      permission_mode=implement_mode),
                    label="plan_implement_mode",
                )
                await self._send(mode_frame(implement_mode, cast("str", self.chat_id)))
                self._queue_implement(stream, plan_fn)
            return None
        if self.chat_id and plan_fn and action in ("reject", *_IMPLEMENT_MODES):
            await chat_writer.submit(
                self.chat_id,
                functools.partial(task_store.update_chat_plan_status, self.chat_id, plan_fn,
                                  "rejected" if action == "reject" else "implemented"),
                label="plan_status",
            )
        if action == "reject":
            # Back to the mode Plan was entered from: the engine follows too
            # (a Codex session leaves its plan collaboration mode).
            await self._handle_mode_change({"mode": pre_plan_mode(self.session_id or ""),
                                            "chat_id": self.chat_id})
        elif implement_mode:
            await self._handle_mode_change({"mode": implement_mode, "chat_id": self.chat_id})
            self.implementing_plan = plan_fn
            if not self.session_id:
                # A dead session: the implement message waits for the warm-up.
                self.implement_queue.append(TurnInput(IMPLEMENT_PROMPT, chat_id=self.chat_id or ""))
                logger.info("WS dashboard: queued implement message for dead session, plan=%s", plan_fn)
            else:
                # A live session between turns (a Codex plan's card): the
                # implement turn starts here, as the person's message.
                await self._handle_chat({"text": IMPLEMENT_PROMPT, "chat_id": self.chat_id or ""})
        return None

    @staticmethod
    async def _plan_row_pending(chat_id: str, filename: str) -> bool:
        """Whether the chat's plan named ``filename`` still waits on its
        review, neither implemented nor rejected (no row: a card from before
        the rows, answered as it was)."""
        from storage.pg import run_db
        rows = await run_db(task_store.get_chat_plans, chat_id)
        row = next((r for r in rows if r.get("filename") == filename), None)
        return row is None or row.get("status") not in ("implemented", "rejected")

    def _queue_implement(self, stream: StreamCtx, plan_fn: str) -> None:
        """The plan's implement message behind the streaming turn: on the
        pump's own list (never a person's queue row), or once that closed on
        the connection's, whose drain turn then carries the plan
        (``implementing_plan``)."""
        item = TurnInput(IMPLEMENT_PROMPT, chat_id=stream.chat_id)
        if stream.pump.queue_message(item) == stream.pump.QUEUE_CLOSED:
            self.implement_queue.append(item)
            self.implementing_plan = plan_fn
        else:
            stream.pump.implementing_plan = plan_fn
