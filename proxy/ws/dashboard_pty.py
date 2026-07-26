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
from services.notifications import notification_manager
from core.session import interactive_session
from core.events.artifact_events import artifact_event_from_perm_item

logger = logging.getLogger("claude-proxy")

# Minimum gap between two ``read_only`` explanations to one socket. A refused
# keystroke must SAY why (a silently dead terminal is the worse failure), but a
# user leaning on a key would otherwise flood the socket with one frame per
# byte.
_READ_ONLY_NOTICE_GAP_S = 2.0


class PtyViewerController:
    """Interactive (PTY) viewer attach/detach: mirrors a live TUI session to
    this socket and forwards its prompts/artifacts/exit."""

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
            "type": "pty_status", "chat_id": sess.chat_id,
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
        # A viewer may watch a shared terminal but never drive one. The
        # PER-AGENT role is what governs (a platform member can be an editor on
        # one agent and a viewer on another), read live so a just-revoked role
        # takes effect immediately.
        from ws.dashboard import _effective_agent_role
        if _effective_agent_role(
            self.user_sub, sess.agent_name, fallback_user=self.user,
        ) == "viewer":
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
        prior. on_perm_event/on_close are single-slot (last viewer wins);
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

        async def _on_pty_output(data: bytes) -> None:
            await self._send({
                "type": "pty_output", "chat_id": vcid, "session_id": vsid,
                "data": base64.b64encode(data).decode("ascii"),
            })

        async def _on_pty_perm(item: dict) -> None:
            # The drainer forwards the session's permission queue here (there is
            # no pump). Blocking prompts surface as ``pty_permission`` (so the
            # agent never hangs); display/file-tools artifacts surface as
            # ``pty_artifact`` (floating windows) carrying
            # the same event the -p pump would emit (shared mapper, no drift).
            et = item.get("event_type", "")
            if et == "permission_prompt":
                frame = {
                    "type": "pty_permission", "kind": "permission",
                    "chat_id": vcid, "session_id": vsid,
                    "request_id": item.get("request_id"),
                    "tool_name": item.get("tool_name", ""),
                    "tool_input": item.get("tool_input", {}),
                }
                if item.get("meeting_agent"):
                    frame["meeting_agent"] = item["meeting_agent"]
                await self._send(frame)
            elif et in ("plan_review", "question"):
                await self._send({
                    "type": "pty_permission", "kind": et,
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
                    # The drainer's persisted row id (final artifacts only) —
                    # the client's stable key for replay-on-open dedupe and
                    # X-dismiss memory (useArtifactWindows).
                    if item.get("db_message_id") is not None:
                        event["db_message_id"] = item["db_message_id"]
                    await self._send({
                        "type": "pty_artifact", "chat_id": vcid,
                        "session_id": vsid, "event": event,
                    })

        async def _on_pty_close(_s, reason: str) -> None:
            await self._send({
                "type": "pty_exit", "chat_id": vcid, "session_id": vsid,
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
            # attributes.
            self._pty_viewer_sid = None
            self._pty_listener = None
            await self._send({
                "type": "pty_exit", "chat_id": vcid, "session_id": vsid,
                "reason": reason,
            })

        async def _on_pty_status(state: str) -> None:
            # the remote PTY transport is reconnecting/reconnected (a
            # satellite WS blip). reconnecting → banner + input pause. reconnected →
            # a clean RE-RENDER first: the inline TUI's relative-cursor repaints
            # leave the mirror mis-aligned across the blip, so reset the xterm and
            # replay the proxy scrollback. This rides BEFORE the buffered gap output
            # (the satellite's control-first writer sends `pty_alive` ahead of the
            # pty lane), so the gap output then appends live onto the fresh screen.
            if state == "reconnected" and sess.pty is not None:
                from core.terminal_queries import strip_clipboard_writes
                await self._send({
                    "type": "pty_output", "chat_id": vcid, "session_id": vsid,
                    # Replay boundary: a buffered OSC 52 copy must not re-fire
                    # into the viewer's clipboard on re-render.
                    "data": base64.b64encode(
                        strip_clipboard_writes(sess.pty.scrollback())
                    ).decode("ascii"),
                    "replay": True, "reset": True,
                })
            await self._send({
                "type": "pty_status", "chat_id": vcid, "session_id": vsid,
                "state": state,
            })

        sess.on_perm_event = _on_pty_perm
        sess.on_close = _on_pty_close
        sess.on_status = _on_pty_status
        # Register the listener BEFORE replaying scrollback so no bytes are lost
        # in the gap; there is no await between the two, so ordering holds. The
        # evict callback makes this single-viewer: a new attach kicks the prior.
        scrollback = sess.add_output_listener(_on_pty_output, on_evict=_evict_this_viewer)
        self._pty_viewer_sid = vsid
        self._pty_listener = _on_pty_output
        # The session's baked TUI theme — the viewer renders its xterm with THIS
        # theme (a dark-seeded TUI in a light xterm paints white-on-white).
        await self._send({
            "type": "pty_status", "chat_id": vcid, "session_id": vsid,
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
                "type": "pty_output", "chat_id": vcid, "session_id": vsid,
                "data": base64.b64encode(scrollback).decode("ascii"),
                "replay": True,
            })
        # If the satellite is mid-reconnect right now, tell the freshly-attached
        # viewer so it shows the reconnecting banner — after
        # the scrollback replay so the banner sits over the current screen.
        if sess.target != "local":
            from core.remote.satellite_connection import get_connection_manager
            if get_connection_manager().is_pty_in_grace(sess.target):
                await self._send({
                    "type": "pty_status", "chat_id": vcid, "session_id": vsid,
                    "state": "reconnecting",
                })
        # dual-control: this dashboard viewer is CLAIMING a session a local
        # `otodock` terminal currently controls → take it over. Modeled
        # as a mini-reconnect: show the "reconnecting" banner + pause input (the
        # server gate already drops dashboard input while otodock_attached), kick
        # the otodock terminal (it keeps the PTY + any in-flight turn alive), and
        # flip control only when the satellite CONFIRMS the detach (or a short
        # fallback timer fires if it's unreachable) — so the two never drive the one
        # PTY at once. The confirm path (detach_local_session) fires "reconnected"
        # → a clean re-render at the dashboard's size.
        if sess.otodock_attached and sess.target != "local":
            from core.remote.satellite_connection import get_connection_manager
            from core.session import otodock_session as _otodock
            sess.notify_status("reconnecting")
            _kick_reason = "taken over by the dashboard — run `otodock --resume` to return"
            asyncio.create_task(
                get_connection_manager().send_local_detach(
                    sess.target, vsid, reason=_kick_reason,
                )
            )
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
        self._pty_viewer_sid = None
        self._pty_listener = None
