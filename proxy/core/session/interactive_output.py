"""A terminal session's viewers: who is attached, who may drive it, and the output
that fans out to them. The bottom of ``InteractiveSession``'s mixin chain.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, cast

from core import placement
from core.background import spawn
from core.session import session_kind
from core.session.interactive_common import (
    _POST_OUTPUT_TAIL_S,
    _RESUME_TAIL_S,
    _SUBMIT_SETTLE_S,
    READY_MIN_S,
    READY_SETTLE_S,
)
from core.terminal_queries import (
    strip_clipboard_writes as _strip_clipboard_writes,
    strip_queries as _strip_terminal_queries,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from core.session.interactive_common import CloseCb, OutputListener
    from core.session.interactive_host import InteractiveHost as _Host
else:
    _Host = object

logger = logging.getLogger("otodock.interactive")


class InteractiveOutputMixin(_Host):
    # The session state this mixin assigns, declared where it assigns it: the
    # type checker reads an attribute a mixin assigns from the mixin first.
    # Annotations only, no class attributes.
    _last_io_at: Any
    _render_started: Any
    _resume_tail_handle: Any
    _settle_handle: Any
    _submit_settle_handle: Any
    _tail_handle: Any
    _viewer_evict: Any
    _viewer_listener: Any
    had_viewer: Any
    on_close: Any
    on_perm_event: Any
    on_status: Any

    @property
    def has_viewer(self) -> bool:
        return bool(self._output_listeners)

    @property
    def is_remote(self) -> bool:
        """Where this PTY runs: a satellite the proxy drives over the WS, or
        the sandbox on this host (``core.placement``; ``target`` is the
        stored value)."""
        return not placement.is_local(self.target)

    def may_drive(self, sender_sub: str) -> bool:
        """Whether ``sender_sub`` may DRIVE this session (type, paste, answer a
        permission prompt, abort a turn).

        The session runs the CLI under the identity of whoever warmed it — its
        JWT, ``OTO_*`` env, platform role and subscription. On a Shared-only
        agent every assigned user can open the same chat, so a second user's
        keystrokes would otherwise act as the warmer: spending their
        subscription and inheriting their platform role (which
        ``agent-creator-mcp``'s endpoint gates on). Input from anyone else is
        refused; changing hands is an explicit take-over, never implicit.

        Exemptions, in order:

        * no sender (server-injected prompts: scheduler wakes, cold submits)
        * a session in a mode word no person picks was built for nobody and
          is driven by nobody (``runs_for_nobody``), a task chat's aside
        * no owner recorded (phone/legacy sessions): nothing to compare against
        * a task chat (``session_kind.is_task_chat_id``) — governed by its OWN
          authorization model (``task_continue_refusal`` is role-based on
          purpose, so
          delegate lanes stay steerable by whoever holds the role); an ownership
          gate on top would break live lane steering.

        Fails OPEN on an unprovable owner, unlike the ``-p`` recycle in
        ``ws/dashboard_chat.py`` which fails closed: there a wrong guess costs
        one respawn, here it would silently swallow a keystroke.
        """
        if not sender_sub:
            return True
        if self.runs_for_nobody:
            return False
        driver = self.sole_driver
        return not driver or sender_sub == driver

    @property
    def runs_for_nobody(self) -> bool:
        """A terminal a wake built the unattended way on a chat people open
        (its mode is a word no person picks): it is watched, never driven. A
        take-over closes it and starts the person's own session."""
        from core.session.session_state import runs_for_nobody
        return runs_for_nobody(self.session_id, self.chat_id, lane=self.chat_is_lane)

    @property
    def sole_driver(self) -> str:
        """The one person who may drive this session (``may_drive``), ``""``
        when anyone may (no owner recorded, a task chat). Only their socket
        can hold the session's prompt slot, so what the drainer forwards
        reaches them alone: a pushed document's token is minted for them
        (``api/hooks/preview.py``)."""
        if not self.user_sub or session_kind.is_task_chat_id(self.chat_id):
            return ""
        return self.user_sub

    # -- viewers (output fan-out) --------------------------------------------
    def add_output_listener(
        self, cb: OutputListener,
        on_evict: Callable[[str], Awaitable[None] | None] | None = None,
        on_close: CloseCb | None = None,
    ) -> bytes:
        """Attach a viewer. Returns the scrollback to replay so the viewer sees
        the current screen (reconnect / late-attach).

        Single controller: when ``on_evict`` is given, this is the driving
        dashboard viewer. It EVICTS the previous controller first (drops that
        one listener and fires its evict callback with reason "superseded")
        so two devices or tabs never drive the same PTY at different sizes,
        which garbles both. A read-only mirror passes no ``on_evict`` and
        survives a controller change; ``on_close`` lets it learn the session
        ended (the controller's notice is the single-slot ``self.on_close``).
        Internal and test listeners pass neither."""
        prev_evict, prev_cb = self._viewer_evict, self._viewer_listener
        supersede = on_evict is not None and prev_evict is not None and prev_cb is not cb
        if supersede:
            self._output_listeners.discard(prev_cb)
            self._listener_close.pop(cast("OutputListener", prev_cb), None)
        self._output_listeners.add(cb)
        if on_close is not None:
            self._listener_close[cb] = on_close
        if on_evict is not None:
            self._viewer_evict = on_evict
            self._viewer_listener = cb
            self.had_viewer = True
        self._note_activity()
        if supersede:
            try:
                res = cast("Callable[[str], Awaitable[None] | None]", prev_evict)("superseded")
                if asyncio.iscoroutine(res):
                    spawn(res,
                          name="evict_superseded_viewer", owner="interactive_session")
            except Exception:
                logger.exception("interactive %s: viewer evict failed", self.session_id[:8])
        if self.pty is None:
            return b""
        sb = self.pty.scrollback()
        # Mirror boundary (replay): strip the app's terminal queries so the
        # attaching viewer has nothing stale to auto-answer — a raw replay makes
        # the mirror re-answer every buffered query on each (re)attach (see
        # core.terminal_queries). Remote rings are pre-stripped at feed
        # (remote_pty._feed_output); a local PTY ring is raw, so strip here.
        # Clipboard writes (OSC 52 set) are replay-stripped for BOTH targets:
        # they pass live (the TUI's copy feature) but a replayed one would
        # overwrite the viewer's clipboard on page open.
        if not self.is_remote:
            sb = _strip_terminal_queries(sb)
        return _strip_clipboard_writes(sb)

    def remove_output_listener(self, cb: OutputListener) -> None:
        self._output_listeners.discard(cb)
        self._listener_close.pop(cb, None)
        if cb is self._viewer_listener:
            self._viewer_evict = None
            self._viewer_listener = None

    def evict_viewer(self, reason: str = "superseded") -> None:
        """dual-control: detach the current dashboard viewer (if any) WITHOUT
        killing the PTY — used when the local `otodock` terminal takes over a live
        session. Fires the viewer's evict callback (which sends it a ``pty_exit
        {reason}`` so it shows the take-over banner and stops driving), then drops
        the listener + the single-slot callbacks so output/perm/status stop
        fanning out to the now-detached socket. Fully SYNCHRONOUS (it only
        SCHEDULES the evict's WS send as a task), so it is safe to call under the
        registry lock without racing ``close()``'s ``on_close`` read."""
        ev, cb = self._viewer_evict, self._viewer_listener
        self._output_listeners.discard(cb)
        self._listener_close.pop(cb, None)  # type: ignore[arg-type]  # no viewer: a None key; typeshed types dict.pop's key as the key type, the call accepts any hashable (library typing gap)
        self._viewer_evict = None
        self._viewer_listener = None
        self.on_perm_event = None
        self.on_close = None
        self.on_status = None
        if ev is not None:
            try:
                res = ev(reason)
                if asyncio.iscoroutine(res):
                    spawn(res, name="evict_viewer", owner="interactive_session")
            except Exception:
                logger.exception("interactive %s: evict_viewer failed", self.session_id[:8])

    def notify_status(self, state: str) -> None:
        """Tell the current viewer the REMOTE PTY transport state changed —
        "reconnecting" on a satellite WS drop, "reconnected" on re-adopt.
        Fire-and-forget; no-op when no viewer wired ``on_status`` (e.g. a
        background interactive task, or a local session)."""
        if self.on_status is None:
            return
        try:
            res = self.on_status(state)
            if asyncio.iscoroutine(res):
                spawn(res, name="status", owner="interactive_session")
        except Exception:
            logger.exception("interactive %s: on_status failed", self.session_id[:8])

    def _fanout_output(self, data: bytes) -> None:
        # The PTY's output → here. Output inside an open turn is the agent
        # working, so an unviewed long turn isn't reaped. Between turns a TUI
        # redraws on its own (a warmup in the same scope rewrites its hook
        # files, a status line), which must not reset the age the reaper and
        # the evictor read; it still breaks the injection gate's quiet. A
        # local otodock terminal types on the satellite, never through
        # write_input, so its echo is the only trace of that input here.
        if self._turn_open or self.otodock_attached:
            self._note_activity()
        else:
            self._last_io_at = time.monotonic()
        # Readiness: output is flowing → (re)arm the settle timer. Ready fires
        # when output goes quiet for READY_SETTLE_S (the TUI finished rendering).
        if not self._ready:
            # Only count output as "the TUI is rendering" once a terminal control
            # sequence (ESC) appears — skips Codex's pre-TUI plain-text warnings so
            # the settle timer measures quiet AFTER the render, not during the warm
            # gap. Claude's Ink TUI emits ESC from its first frame, so this is
            # layer-agnostic; READY_MAX_S remains the hard backstop.
            if not self._render_started and b"\x1b" in data:
                self._render_started = True
            if self._render_started:
                if self._settle_handle is not None:
                    self._settle_handle.cancel()
                # Floor the settle so ready can't fire before READY_MIN_S since
                # spawn: the first post-render quiet is usually the MCP warm,
                # not a live composer.
                delay = max(READY_SETTLE_S,
                            READY_MIN_S - (time.monotonic() - self._spawn_ts))
                self._settle_handle = self._loop.call_later(delay, self._mark_ready)
        # A deferred cold-start submit is waiting for the echo to settle — push
        # its Enter out while the text is still echoing (the max-fallback caps it).
        if self._submit_settle_handle is not None:
            self._submit_settle_handle.cancel()
            self._submit_settle_handle = self._loop.call_later(_SUBMIT_SETTLE_S, self._fire_submit)
        # Post-turn tail: once the TUI is ready, debounce a transcript tail on
        # output quiet so chat_messages + the title land within seconds of a turn
        # (the Stop hook is unreliable; the 60s reaper sweep is just the backstop).
        if self._ready and self.chat_id:
            if self._tail_handle is not None:
                self._tail_handle.cancel()
            self._tail_handle = self._loop.call_later(_POST_OUTPUT_TAIL_S, self._run_post_output_tail)
            # Self-resume detection: output while the turn is CLOSED → check the
            # transcript promptly (the debounce above is starved while output
            # flows). NOT re-armed per chunk — one probe per fuse window, and
            # only while the turn stays closed. Local only: remote turn state
            # rides the forwarded transcript lines.
            if (not self._turn_open and not self.is_remote
                    and self._resume_tail_handle is None):
                self._resume_tail_handle = self._loop.call_later(
                    _RESUME_TAIL_S, self._run_resume_tail)
        # Mirror boundary (live): listeners are dashboard mirrors — strip the
        # app's terminal queries so xterm.js never auto-answers one (the answers
        # would ride pty_input back as phantom keystrokes; see
        # core.terminal_queries). Remote sessions arrive pre-stripped
        # (remote_pty._feed_output); local PTY bytes are stripped here. The
        # readiness/settle/tail logic above intentionally saw the RAW bytes.
        mirror_data = _strip_terminal_queries(data) if not self.is_remote else data
        if not mirror_data:
            return
        for cb in list(self._output_listeners):
            try:
                res = cb(mirror_data)
                if asyncio.iscoroutine(res):
                    spawn(res, name="mirror_output", owner="interactive_session")
            except Exception:
                logger.exception("interactive %s: output listener failed", self.session_id[:8])
