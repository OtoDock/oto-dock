"""Interactive (PTY) viewer attach/detach: mirrors a live TUI session to this
socket and forwards its prompts/artifacts/exit.

PtyViewerController is a mixin of ``DashboardConnection`` (ws/dashboard.py) — methods run
with the connection's full attribute state; nothing here is standalone.
Behavior is pinned by tests/session/test_ws_dashboard_*.
"""

import asyncio
import base64
import logging
import time
from collections import deque
from typing import TYPE_CHECKING

from auth import roles
from core import placement
from core.background import spawn
from core.events.artifact_events import artifact_event_from_perm_item, for_viewer
from core.session import interactive_session
from services.notifications import notification_manager
from ws import wire_events as wire

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ws.dashboard_host import DashboardHost as _Host
else:
    _Host = object

logger = logging.getLogger("otodock")

# Minimum gap between two ``read_only`` explanations to one socket. A refused
# keystroke must SAY why (a silently dead terminal is the worse failure), but a
# user leaning on a key would otherwise flood the socket with one frame per
# byte.
_READ_ONLY_NOTICE_GAP_S = 2.0
# What a viewer's outbox may hold while its socket is paused (a reader slower
# than the terminal): past it the backlog is dropped and the scrollback is
# replayed once the reader catches up, the same re-render a satellite blip
# gets. Larger than the PTY's scrollback ring, so a reader that keeps up
# never sees a replay.
_PTY_OUTBOX_MAX_BYTES = 1024 * 1024

_OUT_DATA = "data"
_OUT_STATUS = "status"
_OUT_REPLAY = "replay"


class _PtyOutbox:
    """One viewer's PTY stream, bounded and in one order: output chunks, the
    transport's statuses and screen replays share one queue and one drain
    task, so no output lands ahead of a replay that should precede it and a
    status never overtakes a replay queued before it.

    The enqueuers are synchronous: the session's output path calls ``push``
    in the same loop step the scrollback ring recorded the chunk, so a
    replay's snapshot, taken when the drain reaches it, holds every chunk
    queued so far. A replay therefore drops the output queued around it
    (it is all in the snapshot) and a chunk pushed while a replay waits is
    not queued. A held outbox (an attach still sending its first replay)
    queues and drains nothing until ``release``. Past the byte limit the
    queued output is dropped and the screen replayed, then ``reconnected``
    re-syncs the viewer's size."""

    def __init__(self, send, replay, send_status, *,
                 limit: int = _PTY_OUTBOX_MAX_BYTES, held: bool = False) -> None:
        self._send = send
        self._replay = replay
        self._send_status = send_status
        self._limit = limit
        self._items: deque[tuple[str, bytes | str]] = deque()
        self._bytes = 0
        self._held = held
        self._cancelled = False
        # A replay is queued and not yet sent: kept as a flag, since push
        # reads it for every chunk in the session's output path.
        self._replay_queued = False
        self.replays = 0
        self.task: asyncio.Task | None = None

    @property
    def replay_pending(self) -> bool:
        return self._replay_queued

    def push(self, data: bytes) -> None:
        if self._cancelled or self.replay_pending:
            return
        if self._bytes + len(data) > self._limit:
            self.request_replay()
            return
        self._items.append((_OUT_DATA, data))
        self._bytes += len(data)
        self._start()

    def push_status(self, state: str) -> None:
        if self._cancelled:
            return
        self._items.append((_OUT_STATUS, state))
        self._start()

    def request_replay(self) -> None:
        """A reset replay of the screen, then ``reconnected``."""
        if self._cancelled:
            return
        self._drop_queued_output()
        if not self._replay_queued:
            self._replay_queued = True
            self._items.append((_OUT_REPLAY, b""))
            self._items.append((_OUT_STATUS, "reconnected"))
        self._start()

    def release(self) -> None:
        if self._cancelled:
            return
        self._held = False
        self._start()

    def cancel(self) -> None:
        self._cancelled = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
        self._items.clear()
        self._bytes = 0
        self._replay_queued = False

    def _drop_queued_output(self) -> None:
        self._items = deque(item for item in self._items if item[0] != _OUT_DATA)
        self._bytes = 0

    def _start(self) -> None:
        if self._held or self._cancelled or not self._items:
            return
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self._drain())

    async def _drain(self) -> None:
        while self._items and not self._cancelled:
            kind, payload = self._items.popleft()
            if kind == _OUT_DATA:
                self._bytes -= len(payload)
                await self._send(payload)
            elif kind == _OUT_STATUS:
                await self._send_status(payload)
            else:
                self._replay_queued = False
                self._drop_queued_output()
                self.replays += 1
                await self._replay()


def _mirror_replay(sess) -> bytes:
    """The scrollback a viewer's reset replay carries, by the attach path's
    rule: a local ring is raw, so the program's terminal queries come out (a
    mirror must not answer them); every ring loses its clipboard writes (a
    replayed copy would overwrite the viewer's clipboard)."""
    from core.terminal_queries import strip_clipboard_writes, strip_queries
    scrollback = sess.pty.scrollback()
    if not sess.is_remote:
        scrollback = strip_queries(scrollback)
    return strip_clipboard_writes(scrollback)



class PtyViewerController(_Host):
    """Interactive (PTY) viewer attach/detach: mirrors a live TUI session to
    this socket and forwards its prompts/artifacts/exit."""

    # The connection state this mixin assigns, declared where it assigns it:
    # the type checker reads an attribute a mixin assigns from the mixin
    # first. Annotations only (strings), no class attributes.
    _pty_listener: "Callable[[bytes], Awaitable[None] | None] | None"
    _pty_outbox: "_PtyOutbox | None"
    _pty_viewer_sid: "str | None"

    async def _send_pty_read_only(self, sess) -> None:
        """Tell this socket its input was refused: the session belongs to
        another user (Shared-only agents put several users on one chat).

        Carries the controller's username so the terminal can name them and
        offer the take-over. Rate-limited per connection."""
        now = time.monotonic()
        last = getattr(self, "_read_only_notice_at", 0.0)
        if now - last < _READ_ONLY_NOTICE_GAP_S:
            return
        self._read_only_notice_at = now
        await self._send({
            "type": wire.PTY_STATUS, "chat_id": sess.chat_id,
            "session_id": sess.session_id, "state": "read_only",
            "controller": sess.username or "",
        })

    async def _handle_pty_takeover(self, msg: dict) -> None:
        """Explicit hand-over of a live interactive session to THIS user.

        A PTY carries live TUI state, so it never changes hands implicitly the
        way a ``-p`` session does — the read-only viewer asks for it. Closing is
        the whole mechanism: the terminal already treats a dead session by
        falling back to the DB view and re-warming with ``--resume`` on the next
        send, and that re-warm binds this user's identity and subscription. A
        take-over that merely re-labelled the controller would leave the running
        process holding the previous user's JWT, role and account.
        """
        sess = interactive_session.get(self.session_id) if self.session_id else None
        if sess is None or not sess.alive or sess.chat_id != msg.get("chat_id"):
            return
        if sess.may_drive(self.user_sub):
            return  # already the controller — nothing to take
        # Closing a colleague's session is driving the chat: a chat that runs
        # as the agent takes the editor tier (the drive gate; the re-warm the
        # take-over leads to is refused below that tier anyway).
        if await self._deny_task_continue(sess.chat_id):
            return
        # A viewer may watch a shared terminal but never drive one. The
        # PER-AGENT role is what governs (a platform member can be an editor on
        # one agent and a viewer on another), read live so a just-revoked role
        # takes effect immediately.
        from auth.providers import acting_role_of
        from storage.pg import run_db
        if not roles.can_write_workspace(await run_db(
            acting_role_of, self.user_sub, sess.agent_name, fallback_user=self.user,
        )):
            await self._send_error("Viewers cannot take over a terminal session")
            return
        logger.info(
            "WS dashboard: PTY take-over chat=%s session=%s from=%s to=%s",
            sess.chat_id, sess.session_id[:8], sess.username or "?",
            self.user.get("username") or "?",
        )
        await interactive_session.close_session(
            sess.session_id, reason="taken_over",
        )

    async def _attach_pty_viewer(self, sess) -> None:
        """Mirror an interactive session's PTY to this socket: stream output as
        base64 ``pty_output`` frames (replaying the scrollback first), forward the
        session's permission queue (drained by the drainer) as ``pty_permission``
        frames, and announce exit. One viewer per socket — re-attach detaches the
        prior. The controller takes the session's single-slot prompt, close
        and status callbacks; a read-only mirror registers only an output
        listener and its own close notice. on_perm_event is single-slot;
        single-viewer for now, multi-viewer is a future extension."""
        self._detach_pty_viewer()
        vsid = sess.session_id
        vcid = sess.chat_id

        # The viewing device is the end-of-turn ping target: record it as this
        # chat's turn origin so the interactive turn-complete notification
        # (interactive_session._fire_turn_notification) routes the ephemeral ping
        # here / to FCM, exactly like the -p send path does. Raw PTY keystrokes
        # never set this otherwise, so without it fire_ephemeral falls to the
        # legacy rule and an actively-connected user gets no ping.
        #
        # Only the CONTROLLER's device claims the origin: on a Shared-only agent
        # a read-only viewer attaching would otherwise re-point the controller's
        # own turn-complete ping at themselves.
        can_drive = sess.may_drive(self.user_sub)
        if vcid and can_drive:
            notification_manager.set_chat_turn_origin(self.user_sub, vcid, self.notify_connection_id)

        async def _send_output(data: bytes) -> None:
            await self._send({
                "type": wire.PTY_OUTPUT, "chat_id": vcid, "session_id": vsid,
                "data": base64.b64encode(data).decode("ascii"),
            })

        async def _replay_screen() -> None:
            # A clean re-render (a reset with the scrollback): the reader
            # fell a full outbox behind, or the remote transport came back
            # (the inline TUI's relative-cursor repaints leave the mirror
            # mis-aligned across a blip). The outbox sends "reconnected"
            # after it, which re-syncs the viewer's size.
            if sess.pty is None:
                return
            await self._send({
                "type": wire.PTY_OUTPUT, "chat_id": vcid, "session_id": vsid,
                "data": base64.b64encode(_mirror_replay(sess)).decode("ascii"),
                "replay": True, "reset": True,
            })

        async def _send_status(state: str) -> None:
            await self._send({
                "type": wire.PTY_STATUS, "chat_id": vcid, "session_id": vsid,
                "state": state,
            })

        # Held until the attach's own replay is out: output arriving while
        # "attached" and the replay are sent waits behind them.
        outbox = _PtyOutbox(_send_output, _replay_screen, _send_status, held=True)

        def _on_pty_output(data: bytes) -> None:
            # Synchronous: the chunk joins the outbox in the loop step the
            # ring recorded it, which the outbox's replays rely on.
            outbox.push(data)

        async def _on_pty_perm(item: dict) -> None:
            # The drainer forwards the session's permission queue here (there is
            # no pump). Blocking prompts surface as ``pty_permission`` (so the
            # agent never hangs); display/file-tools artifacts surface as
            # ``pty_artifact`` (floating windows) carrying
            # the same event the -p pump would emit (shared mapper, no drift).
            et = item.get("event_type", "")
            if et == wire.ITEM_PERMISSION_PROMPT:
                frame = {
                    "type": wire.PTY_PERMISSION, "kind": "permission",
                    "chat_id": vcid, "session_id": vsid,
                    "request_id": item.get("request_id"),
                    "tool_name": item.get("tool_name", ""),
                    "tool_input": item.get("tool_input", {}),
                }
                if item.get("meeting_agent"):
                    frame["meeting_agent"] = item["meeting_agent"]
                await self._send(frame)
            elif et in (wire.ITEM_PLAN_REVIEW, wire.ITEM_QUESTION):
                # Neither reaches a human-driven interactive session any
                # more: the hook lets AskUserQuestion run natively and
                # defers ExitPlanMode to the CLI's own plan dialog
                # (2026-09-10). Kept for the queue's other producers.
                await self._send({
                    "type": wire.PTY_PERMISSION, "kind": et,
                    "chat_id": vcid, "session_id": vsid,
                    "request_id": item.get("request_id"),
                    "tool_name": item.get("tool_name", ""),
                    "tool_input": item.get("tool_input", {}),
                    "plan": item.get("plan", ""),
                    "filename": item.get("filename", ""),
                })
            else:
                # Display/file-tools artifact (images/charts/url/file/media/
                # document_preview) → a PiP floating window. Unlike the pump,
                # there is no turn boundary, so document_preview is forwarded on
                # arrival (the dashboard dedups in-place by file_id).
                event = artifact_event_from_perm_item(item)
                if event is not None:
                    # A push queued before a takeover can reach the next
                    # controller: its token goes only to its own person.
                    event = for_viewer(event, self.user_sub)
                    # The drainer's persisted row id (final artifacts only) —
                    # the client's stable key for replay-on-open dedupe and
                    # X-dismiss memory (useArtifactWindows).
                    if item.get("db_message_id") is not None:
                        event["db_message_id"] = item["db_message_id"]
                    await self._send({
                        "type": wire.PTY_ARTIFACT, "chat_id": vcid,
                        "session_id": vsid, "event": event,
                    })

        async def _on_pty_close(_s, reason: str) -> None:
            await self._send({
                "type": wire.PTY_EXIT, "chat_id": vcid, "session_id": vsid,
                "reason": reason,
                # Non-zero on an abnormal child death (reason "exited") — the
                # terminal renders it so an instant CLI crash is never a
                # silent blank screen.
                "code": getattr(_s, "exit_code", None),
            })

        async def _evict_this_viewer(reason: str = "superseded") -> None:
            # Another viewer took over this live session — tell THIS socket so it
            # stops mirroring (else both render the one PTY at different sizes and
            # garble each other). ``reason`` distinguishes the take-over source:
            # "superseded" = another dashboard tab/device ("opened on another
            # device"); "superseded_otodock" = a local `otodock` terminal took over
            # (dual-control — "opened in a local terminal"). Either way a reload
            # re-claims it (evicting the other). Clears this connection's viewer
            # attributes, and drops what its outbox still held: a queued replay
            # and "reconnected" would make this tab answer with its own size.
            self._pty_viewer_sid = None
            self._pty_listener = None
            outbox.cancel()
            if getattr(self, "_pty_outbox", None) is outbox:
                self._pty_outbox = None
            await self._send({
                "type": wire.PTY_EXIT, "chat_id": vcid, "session_id": vsid,
                "reason": reason,
            })

        def _on_pty_status(state: str) -> None:
            # The remote PTY transport is reconnecting/reconnected (a
            # satellite WS blip). reconnecting → banner + input pause.
            # reconnected → a clean re-render first (the reset replay), then
            # the status. Both ride the outbox in order with the output: the
            # replay's snapshot covers the gap output queued around it, and
            # what arrives after the snapshot follows it.
            if state == "reconnected" and sess.pty is not None:
                outbox.request_replay()
            else:
                outbox.push_status(state)

        # Register the listener BEFORE replaying scrollback so no bytes are lost
        # in the gap: the returned snapshot plus what the held outbox queues
        # from then on is the whole stream, in order.
        # The CONTROLLER's socket is the viewer: its evict callback makes the
        # attach single-viewer (a new attach kicks the prior), and it takes
        # the session's prompt, close and status slots. A socket that may
        # not drive the session mirrors its output only: it evicts nobody,
        # and the controller keeps its prompts (delivered once, they must
        # reach the one who may answer them), its exit notice and its
        # transport status.
        if can_drive:
            sess.on_perm_event = _on_pty_perm
            sess.on_close = _on_pty_close
            sess.on_status = _on_pty_status
            scrollback = sess.add_output_listener(_on_pty_output, on_evict=_evict_this_viewer)
        else:
            scrollback = sess.add_output_listener(_on_pty_output, on_close=_on_pty_close)
        self._pty_viewer_sid = vsid
        self._pty_listener = _on_pty_output
        self._pty_outbox = outbox
        try:
            # The session's baked TUI theme — the viewer renders its xterm with THIS
            # theme (a dark-seeded TUI in a light xterm paints white-on-white).
            await self._send({
                "type": wire.PTY_STATUS, "chat_id": vcid, "session_id": vsid,
                "state": "attached", "tui_theme": sess.tui_theme,
            })
            if not can_drive:
                # Mirroring someone else's session: watch freely, drive nothing
                # until an explicit take-over. Announced right after "attached" so
                # the banner is up before the scrollback replay draws.
                self._read_only_notice_at = 0.0
                await self._send_pty_read_only(sess)
            if scrollback:
                await self._send({
                    "type": wire.PTY_OUTPUT, "chat_id": vcid, "session_id": vsid,
                    "data": base64.b64encode(scrollback).decode("ascii"),
                    "replay": True,
                })
            # If the satellite is mid-reconnect right now, tell the freshly-attached
            # viewer so it shows the reconnecting banner — after
            # the scrollback replay so the banner sits over the current screen.
            if not placement.is_local(sess.target):
                from core.remote.satellite_connection import get_connection_manager
                if get_connection_manager().is_pty_in_grace(sess.target):
                    await self._send({
                        "type": wire.PTY_STATUS, "chat_id": vcid, "session_id": vsid,
                        "state": "reconnecting",
                    })
        finally:
            outbox.release()
        # dual-control: this dashboard viewer is CLAIMING a session a local
        # `otodock` terminal currently controls → take it over. Modeled
        # as a mini-reconnect: show the "reconnecting" banner + pause input (the
        # server gate already drops dashboard input while otodock_attached), kick
        # the otodock terminal (it keeps the PTY + any in-flight turn alive), and
        # flip control only when the satellite CONFIRMS the detach (or a short
        # fallback timer fires if it's unreachable) — so the two never drive the one
        # PTY at once. The confirm path (detach_local_session) fires "reconnected"
        # → a clean re-render at the dashboard's size. A read-only mirror
        # claims nothing.
        if can_drive and sess.otodock_attached and not placement.is_local(sess.target):
            from core.remote.satellite_connection import get_connection_manager
            from core.session import otodock_session as _otodock
            sess.notify_status("reconnecting")
            _kick_reason = "taken over by the dashboard — run `otodock --resume` to return"
            spawn(get_connection_manager().send_local_detach(
                    sess.target, vsid, reason=_kick_reason,
                ), name="send_local_detach", owner="dashboard_pty")
            if sess._otodock_kick_timer is None:
                sess._otodock_kick_timer = asyncio.get_running_loop().call_later(
                    2.0, _otodock.detach_local_session, vsid,
                )
        logger.info("WS dashboard: attached PTY viewer chat=%s session=%s", vcid, vsid[:8])

    def _detach_pty_viewer(self) -> None:
        if self._pty_viewer_sid and self._pty_listener is not None:
            sess = interactive_session.get(self._pty_viewer_sid)
            if sess is not None:
                sess.remove_output_listener(self._pty_listener)
        outbox = getattr(self, "_pty_outbox", None)
        if outbox is not None:
            outbox.cancel()
        self._pty_outbox = None
        self._pty_viewer_sid = None
        self._pty_listener = None
