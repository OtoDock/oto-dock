"""Async notification orchestration layer.

Resolves notification targets, tracks per-connection visibility/idle state, routes delivery
to WebSocket (toast) or native push (FCM/Web Push), and manages notification scheduling.

## Routing model

Each WebSocket connection registers itself with a UUID ``connection_id`` and tracks its own
``active`` flag (driven by the frontend's visibilitychange + 5-min idle timer), an ``away``
flag (visible-but-input-idle — see ``ConnectionInfo``), and ``platform`` (``"web"`` or
``"android"``). A single user may have many simultaneous connections (laptop tab +
Android app + second monitor tab) — state is per-connection, not per-user.

Delivery is mutually exclusive:

* Any connection active (visible + recent input) → WS toast to **all** active connections, **no**
  native push. Inactive connections receive a silent inbox/badge update so their state stays
  current.
* No active connection → native push (FCM + Web Push) handles the alert. Connected-but-inactive
  WSes receive the silent update so the badge is correct when the user returns.

Every fire writes a ``notification_deliveries`` DB row, so the in-app inbox always reflects the
full history regardless of channel.
"""

import asyncio
import contextlib
import logging
import zoneinfo
from dataclasses import dataclass
from datetime import datetime

from storage import notification_store
import config

logger = logging.getLogger("claude-proxy.notifications")


# --- Per-connection state ---


@dataclass
class ConnectionInfo:
    """State for one WebSocket connection (one tab/device per user)."""
    connection_id: str
    queue: asyncio.Queue
    active: bool = True  # overridden by frontend immediately on ws.onopen
    platform: str = "web"  # "web" | "android"
    # Visible-but-input-idle: the dashboard is open on a screen nobody has
    # touched for ~5 min (the FE's `user_idle {away: true}`). Distinct from a
    # plain-idle HIDDEN tab: an away connection's toast/sound plays to an
    # empty chair, so end-of-turn alerts also take the FCM leg for it, while
    # a hidden tab keeps suppressing the buzz (same-machine multitasking).
    away: bool = False


# user_sub → list of all current WS connections for this user
_user_connections: dict[str, list[ConnectionInfo]] = {}


def register_user_connection(
    user_sub: str,
    connection_id: str,
    queue: asyncio.Queue,
    platform: str = "web",
) -> None:
    """Track a new WS connection for this user.

    Frontend sends the actual visibility state (``user_active``/``user_idle``) immediately on
    ``ws.onopen`` — the ``active=True`` default closes the window between register and the first
    visibility message.
    """
    conns = _user_connections.setdefault(user_sub, [])
    # Defensive: don't double-register the same connection_id.
    conns[:] = [c for c in conns if c.connection_id != connection_id]
    conns.append(ConnectionInfo(
        connection_id=connection_id,
        queue=queue,
        active=True,
        platform=platform,
    ))


def unregister_user_connection(user_sub: str, connection_id: str) -> None:
    """Drop the connection from tracking. Removes the user entry if empty."""
    conns = _user_connections.get(user_sub)
    if not conns:
        return
    conns[:] = [c for c in conns if c.connection_id != connection_id]
    if not conns:
        _user_connections.pop(user_sub, None)


def set_connection_active(
    user_sub: str, connection_id: str, active: bool, away: bool = False,
) -> None:
    """Update the active/away flags for one connection (driven by the
    user_active / user_idle WS messages; ``away`` rides ``user_idle`` and is
    forced off for an active connection)."""
    for c in _user_connections.get(user_sub, []):
        if c.connection_id == connection_id:
            c.active = active
            c.away = away and not active
            return


def set_connection_platform(user_sub: str, connection_id: str, platform: str) -> None:
    """Update the platform for one connection (from the client_info message)."""
    for c in _user_connections.get(user_sub, []):
        if c.connection_id == connection_id:
            c.platform = platform
            return


def get_active_connections(user_sub: str) -> list[ConnectionInfo]:
    return [c for c in _user_connections.get(user_sub, []) if c.active]


def get_all_connections(user_sub: str) -> list[ConnectionInfo]:
    return list(_user_connections.get(user_sub, []))


def has_active_connection(user_sub: str) -> bool:
    """True if at least one connection is currently active (visible + recent input)."""
    return any(c.active for c in _user_connections.get(user_sub, []))


def get_connection(user_sub: str, connection_id: str) -> ConnectionInfo | None:
    for c in _user_connections.get(user_sub, []):
        if c.connection_id == connection_id:
            return c
    return None


def chat_status_targets(owner_sub: str, agent: str) -> list[str]:
    """Real users who should see live/read state for a chat owned by
    ``owner_sub``: the owner — or, for a synthetic owner, every user of the
    agent (admins included). Synthetic owners are the shared-only chat owner
    (``agent::<slug>``) AND the scheduler's agent-scope task-chat owner
    (``task::<slug>``) — the latter fans out so scheduled-run pulses reach the
    sidebar's task mode. Sync DB hit on the synthetic paths only."""
    from core.session.visibility import is_shared_chat_owner
    if is_shared_chat_owner(owner_sub) or owner_sub.startswith("task::"):
        try:
            return notification_store.get_agent_user_subs(agent) if agent else []
        except Exception:
            return []
    return [owner_sub] if owner_sub else []


def broadcast_chat_status(owner_sub: str, chat_id: str, status: str, agent: str = "") -> None:
    """Tell every dashboard connection that should see ``chat_id`` that it
    started or ended a turn, so the sidebar live-dot is correct even for chats
    generating in the BACKGROUND. Emitted by the pump on every turn start/end
    (viewed, detached, or headless) and by interactive sessions on turn-open
    transitions. ``owner_sub`` is the chat row's owner; pass ``agent`` so
    shared-only chats (synthetic ``agent::`` owner) fan out to the agent's
    users instead of nobody. Best-effort."""
    if not chat_id:
        return
    for sub in chat_status_targets(owner_sub, agent):
        for c in _user_connections.get(sub, []):
            try:
                c.queue.put_nowait({"type": "chat_status", "chat_id": chat_id, "status": status})
            except Exception as e:
                logger.debug("chat_status broadcast: %s", e)


def broadcast_chat_read(owner_sub: str, chat_id: str, agent: str = "") -> None:
    """Tell every dashboard connection that should see ``chat_id`` that its
    unread marker cleared (someone opened the chat) — same fan-out as
    ``broadcast_chat_status``, so a shared-only chat clears on every user's
    sidebar and a user's other tabs stay in sync. Best-effort."""
    if not chat_id:
        return
    for sub in chat_status_targets(owner_sub, agent):
        for c in _user_connections.get(sub, []):
            try:
                c.queue.put_nowait({"type": "chat_read", "chat_id": chat_id})
            except Exception as e:
                logger.debug("chat_read broadcast: %s", e)


def broadcast_goal_update(user_sub: str, chat_id: str, goal: dict | None) -> None:
    """Tell ALL of a user's dashboard connections that ``chat_id``'s codex
    thread goal changed OUTSIDE a turn (codex accounts goal progress at turn
    stop, so the final update — often the completion — lands after
    turn/completed with no pump to carry it). ``goal=None`` clears the panel.
    The frontend's per-chat frame gate scopes it to the viewing client.
    Best-effort, sync (mirrors broadcast_chat_status)."""
    if not chat_id or not user_sub:
        return
    for c in _user_connections.get(user_sub, []):
        try:
            c.queue.put_nowait({"type": "goal_update", "chat_id": chat_id, "goal": goal})
        except Exception as e:
            logger.debug("goal_update broadcast: %s", e)


def broadcast_chat_rows(user_sub: str, chat_id: str, agent: str = "") -> None:
    """Tell a user's dashboard connections that new HISTORY rows were persisted
    for an interactive chat (transcript tail batch). The frontend uses it to
    live-refresh an open rich-history view (the terminal ⇄ transcript toggle);
    everything else ignores it. Deliberately payload-free (no row content) —
    the viewer refetches through the normal authorized GET. Best-effort, sync
    (mirrors broadcast_chat_title)."""
    if not chat_id or not user_sub:
        return
    for c in _user_connections.get(user_sub, []):
        try:
            c.queue.put_nowait(
                {"type": "chat_rows", "chat_id": chat_id, "agent": agent})
        except Exception as e:
            logger.debug("chat_rows broadcast: %s", e)


def broadcast_chat_title(user_sub: str, chat_id: str, title: str, agent: str = "") -> None:
    """Tell every dashboard connection that should see ``chat_id`` that it was
    (re)titled, so the sidebar + Active-now rows update without waiting for a
    navigation refetch. Used by the transcript tailer to surface the backfilled
    title of an interactive chat (which can't title at send-time). Same
    ``title_updated`` event the headless send-path emits per-socket; the
    frontend's onTitleUpdated refetches the list. Fan-out mirrors
    ``broadcast_chat_status`` — pass ``agent`` so a shared-only chat's synthetic
    ``agent::`` owner reaches the agent's users instead of nobody. Best-effort."""
    if not chat_id or not user_sub:
        return
    for sub in chat_status_targets(user_sub, agent):
        for c in _user_connections.get(sub, []):
            try:
                c.queue.put_nowait({"type": "title_updated", "chat_id": chat_id, "title": title})
            except Exception as e:
                logger.debug("title_updated broadcast: %s", e)


# --- Per-chat turn origin (which device sent the last user prompt) ---

# chat_id → (connection_id, platform). Drives end-of-turn alert routing:
# browser-origin chats ping THAT browser (even with the tab hidden) and never
# fire Android FCM; app-origin chats fire FCM when the app is backgrounded.
# In-memory — after a proxy restart fire_ephemeral falls back to the legacy
# activity-based rule until the chat's next user send.
_chat_turn_origin: dict[str, tuple[str, str]] = {}


def set_chat_turn_origin(user_sub: str, chat_id: str, connection_id: str) -> None:
    """Record which connection (device) initiated the chat's current turn.
    Called at user-send time (warmup first prompt / chat / queued message)."""
    if not chat_id or not connection_id:
        return
    conn = get_connection(user_sub, connection_id)
    if conn:
        _chat_turn_origin[chat_id] = (connection_id, conn.platform)


# --- Target resolution ---


def resolve_targets(scope: str, target: str | None) -> list[str]:
    """Resolve notification scope + target to a list of user_sub IDs."""
    if scope == "user":
        return [target] if target else []
    elif scope == "agent":
        if not target:
            return []
        return notification_store.get_agent_user_subs(target)
    elif scope == "global":
        return notification_store.get_all_user_subs()
    elif scope == "admin":
        return notification_store.get_admin_user_subs()
    return []


# --- Notification firing ---


async def fire_notification(
    title: str,
    body: str,
    severity: str = "info",
    scope: str = "user",
    target: str | None = None,
    source: str = "mcp",
    source_id: str | None = None,
    notification_id: str | None = None,
    agent_slug: str | None = None,
    chat_id: str | None = None,
) -> list[dict]:
    """Fire a notification to all resolved targets.

    Creates a delivery record per user (so the inbox is always populated regardless of channel)
    then routes to WS / native push per the mutually-exclusive policy in ``_deliver_to_user``.
    """
    user_subs = await asyncio.to_thread(resolve_targets, scope, target)
    if not user_subs:
        logger.warning(
            f"No targets resolved for notification: scope={scope}, target={target}"
        )
        return []

    deliveries = []
    for user_sub in user_subs:
        delivery = await asyncio.to_thread(
            notification_store.create_delivery,
            user_sub=user_sub,
            title=title,
            body=body,
            severity=severity,
            scope=scope,
            source=source,
            notification_id=notification_id,
            agent_slug=agent_slug,
            chat_id=chat_id,
        )
        deliveries.append(delivery)
        await _deliver_to_user(user_sub, delivery)

    # Update fired count if this came from a stored notification definition
    # (immediate-fire from create, scheduled fire, or manual /fire endpoint).
    # Then hard-delete the definition row if it's a one-time notification —
    # matches one-time task behaviour. Delivery records in
    # notification_deliveries are independent and remain in the user's inbox.
    if notification_id:
        await asyncio.to_thread(
            notification_store.update_notification_fired, notification_id
        )
        notif_row = await asyncio.to_thread(
            notification_store.get_notification, notification_id
        )
        if notif_row and notif_row.get("notification_type") == "one_time":
            if config.SCHEDULER_MODE != "standalone":
                unregister_notification(notification_id)
            await asyncio.to_thread(
                notification_store.delete_notification, notification_id
            )
            logger.debug(f"Cleaned up fired one-time notification: {notification_id}")

    logger.info(
        f"Notification fired: title={title!r}, severity={severity}, "
        f"scope={scope}, targets={len(deliveries)}"
    )
    return deliveries


def _install_id() -> str:
    """This proxy's stable install id (the relay identity), tagged into every push
    so the Android app can route a notification to the matching installation. Empty
    string if unavailable — old apps ignore it and route to the active install."""
    try:
        from services.billing.relay_client import get_install_id
        return get_install_id()
    except Exception:
        return ""


async def fire_ephemeral(
    user_sub: str, title: str, body: str, chat_id: str | None = None,
    *, interactive: bool = False, cli_attached: bool = False,
) -> None:
    """Fire an ephemeral turn-complete signal, routed to the device that
    STARTED the turn (recorded by ``set_chat_turn_origin`` at send time):

    * **Browser-origin chat** → a ``turn_complete`` WS frame to that browser
      connection — the frontend pings even when the tab is hidden or the chat
      is backgrounded — and normally no Android FCM. If the browser connection
      is gone, the alert is dropped (a browser chat never buzzes the phone).
      EXCEPTION: an ``away`` origin (dashboard visibly open, no input for
      ~5 min — the toast just played to an empty chair) falls through to the
      presence/FCM path IN ADDITION to the frame, so the phone hears about it
      unless another connection is actively used.
    * **App-origin chat** → nothing while the app is foregrounded (the open
      view shows the result); FCM when the app is backgrounded/disconnected.
      The Android side keeps the shade entry visible until the app next comes
      to the foreground — see ``NotificationService.handleEphemeral``.
    * **No recorded origin** (server/scheduler turn, or the proxy restarted
      since the send) → legacy activity rule: an actively-engaged connection
      means the in-app ping covers it; otherwise FCM.

    ``interactive=True`` (terminal turn ends, which have no pump and often no
    origin) overlays a presence rule on the FCM/silent fallthroughs: while ANY
    dashboard connection is active (visible + recent input), the alert takes
    the in-app path — a ``turn_complete`` frame to every active connection —
    instead of buzzing the phone. An active Android connection counts as
    presence (the foreground app ignores the frame and shows the result).
    Without active presence the FCM leg runs, and (interactive only, not
    cli_attached — that turn just rendered on the live terminal) every idle
    WEB connection still gets the frame so an open-but-unattended dashboard
    toasts alongside the push.

    ``cli_attached=True`` (an ``otodock``-CLI terminal owns the session) also
    IGNORES the recorded origin: a CLI attachment is not dashboard presence,
    and a stale origin left by a superseded dashboard viewer must never
    swallow the push. Without active dashboard presence the FCM always fires,
    even while the CLI terminal is open on the remote machine.
    """
    frame = {
        "type": "turn_complete", "chat_id": chat_id,
        "title": title, "body": body,
    }
    origin = None
    origin_away = False
    if chat_id and not cli_attached:
        origin = _chat_turn_origin.get(chat_id)
    if origin:
        conn_id, platform = origin
        conn = get_connection(user_sub, conn_id)
        if platform != "android":
            if not conn:
                logger.debug(
                    f"turn_complete dropped: origin browser connection gone (chat={chat_id})"
                )
                return
            await _safe_put(conn.queue, frame)
            if not conn.away:
                return
            # Origin dashboard visibly open but input-idle: the frame above
            # keeps the local toast/sound, and the alert ALSO falls through
            # to the presence/FCM path so the phone hears about it.
            origin_away = True
        elif conn and conn.active:
            return  # app in the foreground — the open view shows the result
        # app-origin backgrounded / away browser-origin → overlay / FCM below
    if interactive or cli_attached:
        active = get_active_connections(user_sub)
        if active:
            for c in active:
                await _safe_put(c.queue, frame)
            return
        if interactive and not cli_attached:
            # No active presence → the FCM leg below runs; idle-but-open web
            # dashboards still get the frame so they toast alongside the push
            # (the user may be looking after all). Skipped for cli_attached —
            # that turn just rendered on the live otodock terminal. The away
            # origin conn was already framed above; android conns alert via
            # FCM (their FE ignores the frame anyway).
            for c in get_all_connections(user_sub):
                if c.platform != "web":
                    continue
                if origin_away and c.connection_id == origin[0]:
                    continue
                await _safe_put(c.queue, frame)
    elif (not origin or origin_away) and has_active_connection(user_sub):
        return  # legacy rule: an engaged device's in-app ping covers it

    try:
        from services.notifications.push_sender import send_fcm
        from storage import notification_store as ns
        # Deep link for the tap — same route rules as _deliver_to_user's
        # click_url. Ephemeral pushes historically carried NO link, so tapping
        # an end-of-turn notification just foregrounded the app on whatever
        # chat it already showed (live-observed 2026-07-11).
        click_url = "/"
        if chat_id:
            # Task chats open the chat page with task mode toggled on; the
            # /runs/{id} resolver redirect stays the agent-less fallback.
            if chat_id.startswith("task-"):
                click_url = f"/runs/{chat_id[5:]}"
            try:
                from storage import database as task_store
                _chat = await asyncio.to_thread(task_store.get_chat, chat_id)
                _agent = (_chat or {}).get("agent")
                if _agent:
                    click_url = (f"/chat/{_agent}/{chat_id}?tasks=1"
                                 if chat_id.startswith("task-")
                                 else f"/chat/{_agent}/{chat_id}")
            except Exception:
                pass  # link is best-effort — the push itself still matters
        subscriptions = await asyncio.to_thread(ns.get_push_subscriptions, user_sub)
        for sub in subscriptions:
            if sub["platform"] == "android":
                await send_fcm(sub["subscription_data"], {
                    "title": title,
                    "body": body,
                    "severity": "info",
                    "ephemeral": True,
                    "click_url": click_url,
                    "install_id": _install_id(),
                })
        logger.debug(f"Ephemeral FCM sent to {user_sub}: {title}")
    except ImportError:
        logger.debug("push_sender not available yet, skipping ephemeral push")
    except Exception as e:
        logger.warning(f"Failed to send ephemeral push to {user_sub}: {e}")


def _build_delivery_payload(delivery: dict) -> dict:
    """Shape the delivery dict for the WS event body. Shared by both notification and
    notification_silent events so the frontend can treat the payload identically."""
    return {
        "id": delivery["id"],
        "notification_id": delivery.get("notification_id"),
        "title": delivery["title"],
        "body": delivery["body"],
        "severity": delivery["severity"],
        "scope": delivery["scope"],
        "source": delivery["source"],
        "delivered_at": delivery["delivered_at"],
        "agent_slug": delivery.get("agent_slug"),
        "chat_id": delivery.get("chat_id"),
    }


async def _safe_put(queue: asyncio.Queue, message: dict) -> bool:
    """Try ``queue.put(...)`` and swallow + log any exception. Returns True on success."""
    try:
        await queue.put(message)
        return True
    except Exception as e:
        logger.debug(f"WS queue put failed: {e}")
        return False


async def broadcast_file_updated(
    agent_slug: str, rel_path: str, *, source: str = "disk",
    exclude_user_sub: str = "", pin: bool = False,
) -> None:
    """Push a lightweight ``file_updated`` event to the active dashboard
    connections of users assigned to ``agent_slug`` who are allowed to see
    ``rel_path`` (per-user isolation), so an open Collabora preview / workspace
    file-tree refreshes.

    NOT a notification — no inbox row, no toast, no sound, no DB write. ``source``
    distinguishes a Collabora save (``"collabora"`` — already live-merged among
    the humans editing it, so a peer's open Collabora session doesn't need a
    reload) from an agent / disk write (``"disk"`` — which a live Collabora
    session doesn't know about). The CLIENT decides whether to reload based on
    ``source`` + its own dirty state, and ignores the event for files it doesn't
    have open. ``exclude_user_sub`` skips the writer (no point refreshing their
    own save). Best-effort; never raises."""
    if not agent_slug or not rel_path:
        return
    try:
        from core.remote.file_sync import should_sync_to_target
        from storage import database as task_store

        user_subs = await asyncio.to_thread(resolve_targets, "agent", agent_slug)
        if not user_subs:
            return
        # base64url of the AGENTS_DIR-relative path == api.media.wopi.encode_file_id,
        # so the client can match this event to an open Collabora preview by its
        # file_id without a path round-trip.
        import base64
        file_id = base64.urlsafe_b64encode(
            f"{agent_slug}/{rel_path}".encode()
        ).decode().rstrip("=")
        msg = {
            "type": "file_updated",
            "agent_slug": agent_slug,
            "rel_path": rel_path,
            "file_id": file_id,
            "source": source,
        }
        if pin:
            # Dock pin membership changed for this path (file pinned or
            # unpinned) — clients refresh the pins list, not just content.
            msg["pin"] = True

        def _role_and_name(user_sub: str) -> tuple[str, str]:
            # Effective per-agent role (mirrors ws.dashboard._effective_agent_role):
            # platform admins are "admin"; otherwise the per-agent assignment.
            u = task_store.get_user(user_sub) or {}
            if u.get("role") == "admin":
                role = "admin"
            else:
                role = (task_store.get_user_agent_roles(user_sub) or {}).get(
                    agent_slug, "viewer",
                )
            return role, (task_store.get_username_by_sub(user_sub) or "")

        for user_sub in user_subs:
            if exclude_user_sub and user_sub == exclude_user_sub:
                continue
            active = [c for c in _user_connections.get(user_sub, []) if c.active]
            if not active:
                continue
            # Per-user isolation: never tell a user about a path they can't see
            # (another user's users/{u}/ file, or config/ for a non-owner) —
            # the same predicate the workspace fan-out applies.
            role, username = await asyncio.to_thread(_role_and_name, user_sub)
            if not should_sync_to_target(rel_path, username, role):
                continue
            for c in active:
                await _safe_put(c.queue, msg)
    except Exception:
        logger.debug(
            "broadcast_file_updated failed for %s/%s",
            agent_slug, rel_path, exc_info=True,
        )


async def _deliver_to_user(user_sub: str, delivery: dict) -> None:
    """Route a delivery via WS toast OR native push — never both.

    * Active connections → WS ``notification`` event (toast + sound on that device).
    * Inactive connections → WS ``notification_silent`` event (inbox + badge only, no alert).
    * No active connection at all → native push (FCM + Web Push) handles the alert.

    The DB row was already written in ``fire_notification`` before this is called, so the inbox
    always has the entry regardless of which path runs.
    """
    payload = _build_delivery_payload(delivery)
    active_conns = get_active_connections(user_sub)
    all_conns = get_all_connections(user_sub)
    inactive_conns = [c for c in all_conns if not c.active]

    if active_conns:
        # WS toast to every actively-engaged device.
        for c in active_conns:
            ok = await _safe_put(c.queue, {"type": "notification", "delivery": payload})
            if ok:
                logger.debug(
                    f"Notification delivered via WS (active) to {user_sub} "
                    f"conn={c.connection_id[:8]} platform={c.platform}: {delivery['title']}"
                )
        # Silent inbox update to any inactive WS so its badge stays in sync.
        for c in inactive_conns:
            await _safe_put(c.queue, {"type": "notification_silent", "delivery": payload})
        return

    # No active connection — native push is the alert channel.
    push_attempted = False
    try:
        from services.notifications.push_sender import send_to_user
        _agent = delivery.get("agent_slug")
        _cid = delivery.get("chat_id")
        if delivery.get("source") == "file_conflict" and _agent:
            # File-conflict notifications deep-link to the workspace Recover bin
            # (no chat_id); AgentChat reads ?recover=1 and opens the modal.
            click_url = f"/chat/{_agent}?recover=1"
        elif _cid and _cid.startswith("task-"):
            # Task notifications deep-link to the chat page with task mode on;
            # without an agent slug, the /runs/{run_id} resolver redirects.
            click_url = (f"/chat/{_agent}/{_cid}?tasks=1" if _agent
                         else f"/runs/{_cid[5:]}")
        elif _agent and _cid:
            click_url = f"/chat/{_agent}/{_cid}"
        else:
            click_url = "/"
        await send_to_user(user_sub, {
            "title": delivery["title"],
            "body": delivery["body"],
            "delivery_id": delivery["id"],
            "severity": delivery["severity"],
            "click_url": click_url,
            "install_id": _install_id(),
        })
        push_attempted = True
        logger.debug(
            f"Notification delivered via native push to {user_sub} "
            f"(no active connection): {delivery['title']}"
        )
    except ImportError:
        logger.debug("push_sender not available yet, skipping push")
    except Exception as e:
        logger.warning(f"Push delivery failed to {user_sub}: {e}")

    # Silent inbox update to all connected-but-inactive WSes so the badge/inbox stay current
    # when the user returns to the dashboard.
    for c in all_conns:
        await _safe_put(c.queue, {"type": "notification_silent", "delivery": payload})

    if not all_conns and not push_attempted:
        logger.debug(f"No delivery channel for {user_sub} — entry remains in DB inbox only")


# --- Scheduling ---

_scheduler_ref = None  # Set at startup


def start() -> None:
    """Initialize notification system. Called at proxy startup after scheduler.start()."""
    if config.SCHEDULER_MODE == "standalone":
        logger.info(
            "Notification manager started "
            "(scheduling handled by standalone scheduler)"
        )
        return

    from services.scheduler import scheduler as sched_module
    global _scheduler_ref
    _scheduler_ref = sched_module.get_scheduler()

    # DB tables initialized in app.py via pg_schema.init_schema()

    # Schedule all enabled notifications
    _schedule_all_notifications()

    logger.info("Notification manager started")


def _schedule_all_notifications() -> None:
    """Register all enabled notifications with APScheduler."""
    if not _scheduler_ref:
        logger.warning("Scheduler not available, skipping notification scheduling")
        return

    notifications = notification_store.list_notifications(enabled_only=True)
    scheduled = 0
    for n in notifications:
        if _register_notification(n):
            scheduled += 1

    if scheduled:
        logger.info(f"Scheduled {scheduled} notifications")


def _register_notification(notif: dict) -> bool:
    """Register a single notification with APScheduler. Returns True if registered."""
    if not _scheduler_ref:
        return False

    from apscheduler.triggers.date import DateTrigger

    from services.scheduler import scheduler_triggers

    job_id = f"notif_{notif['id']}"
    ntype = notif.get("notification_type", "one_time")

    # Resolve the row's TZ — user_tz (browser-snapshotted at create) overrides
    # platform default. Invalid IANA → fall back to platform silently.
    tz_name = notif.get("user_tz") or config.get_platform_timezone()
    try:
        notif_tz = zoneinfo.ZoneInfo(tz_name)
    except Exception:
        logger.warning(f"Invalid user_tz {tz_name!r} on notification {notif['id']}; using platform TZ")
        notif_tz = config.get_platform_tz()

    try:
        if ntype == "recurring" and notif.get("schedule"):
            trigger = scheduler_triggers.build_cron_trigger(notif["schedule"], notif_tz)
        elif ntype == "recurring" and notif.get("interval_seconds") is not None:
            # Anchor start_date at `created_at + interval_seconds`. Mirrors the
            # task-side anchor (see scheduler_triggers.build_interval_trigger).
            # Normalise a datetime created_at → ISO before handing to the helper.
            created_at = notif.get("created_at")
            if created_at is not None and not isinstance(created_at, str):
                created_at = created_at.isoformat()
            trigger = scheduler_triggers.build_interval_trigger(
                notif["interval_seconds"], created_at, notif_tz,
            )
        elif ntype == "one_time" and notif.get("run_at"):
            run_date = datetime.fromisoformat(notif["run_at"])
            if run_date.tzinfo is None:
                # Naive datetimes are assumed to be in the row's TZ (user_tz
                # snapshot or platform fallback). Agents write local time.
                run_date = run_date.replace(tzinfo=notif_tz)
            if run_date < datetime.now(run_date.tzinfo):
                logger.debug(f"Skipping past notification: {notif['id']}")
                return False
            trigger = DateTrigger(run_date=run_date)
        else:
            # Immediate or no schedule — don't register with scheduler
            return False

        _scheduler_ref.add_job(
            _fire_scheduled_notification,
            trigger=trigger,
            args=[notif["id"]],
            id=job_id,
            name=f"notif: {notif.get('title', notif['id'])}",
            misfire_grace_time=300,
            coalesce=True,
            replace_existing=True,
        )
        return True
    except Exception as e:
        logger.warning(f"Failed to schedule notification {notif['id']}: {e}")
        return False


def unregister_notification(notification_id: str) -> None:
    """Remove a notification from APScheduler."""
    if config.SCHEDULER_MODE == "standalone" or not _scheduler_ref:
        return
    job_id = f"notif_{notification_id}"
    with contextlib.suppress(Exception):  # job may not exist
        _scheduler_ref.remove_job(job_id)


async def _fire_scheduled_notification(notification_id: str) -> None:
    """APScheduler callback: fire a scheduled notification."""
    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id
    )
    if not notif or not notif.get("enabled"):
        return

    await fire_notification(
        title=notif["title"],
        body=notif["body"],
        severity=notif["severity"],
        scope=notif["scope"],
        target=notif.get("target"),
        source=notif["source"],
        source_id=notif.get("source_id"),
        notification_id=notification_id,
        agent_slug=notif.get("agent_slug"),
        chat_id=notif.get("chat_id"),
    )


def schedule_new_notification(notif: dict) -> bool:
    """Schedule a newly created notification. Called after API creates it."""
    if config.SCHEDULER_MODE == "standalone":
        return False  # Standalone scheduler picks it up via DB sync
    return _register_notification(notif)


async def pause_notification(notification_id: str) -> bool:
    """Set enabled=FALSE in DB and unregister from APScheduler (embedded mode).

    Returns True if the notification exists. Idempotent.
    Standalone mode picks up the change via DB sync (≤ SCHEDULER_SYNC_INTERVAL).
    Defence-in-depth: ``_fire_scheduled_notification`` already short-circuits
    on ``enabled=FALSE``, so a stale fire during the sync window is harmless.
    """
    ok = await asyncio.to_thread(
        notification_store.set_notification_enabled, notification_id, False,
    )
    if ok and config.SCHEDULER_MODE != "standalone":
        unregister_notification(notification_id)
    return ok


async def resume_notification(notification_id: str) -> bool:
    """Set enabled=TRUE in DB and re-register with APScheduler (embedded mode).

    Returns True if the notification exists. For one-time notifications whose
    ``run_at`` is in the past, ``_register_notification`` returns False — the
    row stays enabled but no job is scheduled. The user can press 'Fire Now'
    in the UI to fire manually.
    Standalone mode picks up the change via DB sync.
    """
    ok = await asyncio.to_thread(
        notification_store.set_notification_enabled, notification_id, True,
    )
    if not ok or config.SCHEDULER_MODE == "standalone":
        return ok
    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id,
    )
    if notif and notif.get("enabled"):
        _register_notification(notif)
    return ok


async def delete_notification(notification_id: str) -> bool:
    """Hard-delete: unregister from APScheduler then delete the DB row.

    Returns True if the row was deleted. Standalone mode picks up the row
    removal via DB sync; the cleanup loop drops the orphaned APScheduler job.
    """
    if config.SCHEDULER_MODE != "standalone":
        unregister_notification(notification_id)
    return await asyncio.to_thread(
        notification_store.delete_notification, notification_id,
    )


_NOTIF_TIMING_FIELDS = {"schedule", "run_at", "interval_seconds", "user_tz"}


async def update_notification(notification_id: str, fields: dict) -> tuple[bool, str | None]:
    """Apply a partial update + reschedule if timing changed.

    Validates cron / ISO datetime, normalises mutually exclusive timing
    fields, auto-derives ``notification_type`` (recurring when schedule is
    set, one_time when run_at is set), updates DB, then re-registers the
    APScheduler job in embedded mode.

    Returns ``(ok, error_message)``. ``error_message`` is non-empty when
    validation failed (caller maps to HTTP 400).
    """
    from services.scheduler import scheduler_triggers

    notif = await asyncio.to_thread(
        notification_store.get_notification, notification_id,
    )
    if not notif:
        return False, None  # caller maps to 404

    payload = dict(fields)

    # Validate user_tz first — drives naive run_at parsing below.
    edit_tz_name: str
    if "user_tz" in payload and payload["user_tz"] is not None:
        try:
            zoneinfo.ZoneInfo(payload["user_tz"])
        except Exception as e:
            return False, f"Invalid user_tz: {e}"
        edit_tz_name = payload["user_tz"]
    else:
        edit_tz_name = notif.get("user_tz") or config.get_platform_timezone()
    try:
        edit_tz = zoneinfo.ZoneInfo(edit_tz_name)
    except Exception:
        edit_tz = config.get_platform_tz()

    # Validate cron string against the post-edit TZ (build_cron_trigger, so
    # the standard-cron day-of-week remap validates too — never from_crontab)
    if "schedule" in payload and payload["schedule"] is not None:
        try:
            scheduler_triggers.build_cron_trigger(payload["schedule"], edit_tz)
        except Exception as e:
            return False, f"Invalid cron schedule: {e}"

    # Validate ISO datetime — naive interpreted in post-edit TZ (user_tz or
    # platform fallback). Aligns with _register_notification's behaviour.
    if "run_at" in payload and payload["run_at"] is not None:
        try:
            run_date = datetime.fromisoformat(payload["run_at"])
            if run_date.tzinfo is None:
                payload["run_at"] = run_date.replace(tzinfo=edit_tz).isoformat()
        except Exception as e:
            return False, f"Invalid run_at: {e}"

    # Validate interval bounds when present (lazy import to keep this module
    # cheap to import for the standalone scheduler path).
    if "interval_seconds" in payload and payload["interval_seconds"] is not None:
        from services.scheduler.scheduler import _validate_interval_seconds
        err = _validate_interval_seconds(payload["interval_seconds"])
        if err:
            return False, err

    # Mutual exclusivity + auto-derive notification_type
    if payload.get("schedule"):
        payload["interval_seconds"] = None
        payload["run_at"] = None
        payload["notification_type"] = "recurring"
    elif payload.get("interval_seconds"):
        payload["schedule"] = None
        payload["run_at"] = None
        payload["notification_type"] = "recurring"
    elif payload.get("run_at"):
        payload["schedule"] = None
        payload["interval_seconds"] = None
        payload["notification_type"] = "one_time"

    timing_changed = any(k in payload for k in _NOTIF_TIMING_FIELDS)

    ok = await asyncio.to_thread(
        notification_store.update_notification, notification_id, payload,
    )
    if not ok:
        return False, None

    # Re-register only when timing changed and we're in embedded mode.
    if timing_changed and config.SCHEDULER_MODE != "standalone":
        refreshed = await asyncio.to_thread(
            notification_store.get_notification, notification_id,
        )
        if refreshed and refreshed.get("enabled"):
            unregister_notification(notification_id)
            _register_notification(refreshed)
    return True, None
