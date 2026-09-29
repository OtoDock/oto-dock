"""The platform catalog, v1 (APPS.md "Platform catalog"): what an app
may take from the platform without waking an MCP.

Three delivery kinds are named; two ship here. FEEDS are pushed state a
page subscribes to with ``otodock.feed``: the host page answers the
snapshot as the viewer over REST (``GET /v1/apps/{id}/catalog/{feed}``)
and the platform pushes deltas as ``catalog`` frames on the per-connection
live queue, each with a ``seq`` per (user, agent, feed) so a dropped delta
is a visible gap and the host re-requests the snapshot. METHODS are
request and response (``otodock.platform``), answered as the viewer over
``POST /v1/apps/{id}/catalog/{method}``. EVENTS (a server handler woken)
arrive with the app runtime. Every entry is declared in the signed
manifest and approved on the card; every read is the viewer's own slice;
external links never get the catalog.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Iterable
from datetime import datetime, timezone

from storage import database as task_store
from core import placement
from core.session import session_kind
from core.session.visibility import is_phone_chat_owner, is_shared_chat_owner
from ws import wire_events as wire
from core import layout

logger = logging.getLogger("claude-proxy.apps")

# Feeds answered by the host page from its own state (they existed before
# the catalog); the rest are answered by the platform.
CLIENT_FEEDS = frozenset({"active_chats", "project_lanes"})

FEEDS: dict[str, dict] = {
    "active_chats": {
        "description": "the chats generating right now across the agents you may see",
        "min_role": "viewer",
    },
    "project_lanes": {
        "description": "the lanes of this project (a project dashboard only)",
        "min_role": "viewer",
    },
    "sessions": {
        "description": "this agent's chats and delegation lanes you may open, with their live status",
        "min_role": "viewer",
    },
    "tasks": {
        "description": "this agent's scheduled and trigger tasks you may see, with their schedule in words, next run and last run",
        "min_role": "viewer",
    },
    "notifications": {
        "description": "your own notification inbox",
        "min_role": "viewer",
    },
    "trigger_fires": {
        "description": "this agent's triggers you may see and when they last fired",
        "min_role": "viewer",
    },
    "file_changes": {
        "description": "workspace files that changed while the app is open (native sandbox writes emit nothing until the next sync)",
        "min_role": "viewer",
    },
    "check_verdicts": {
        "description": "the verdicts of this agent's checks you may see (CHECKS.md): your own sessions' and the agent-scope ones",
        "min_role": "viewer",
    },
    "checks": {
        "description": "this agent's checks you may see (CHECKS.md), each with its last verdict",
        "min_role": "viewer",
    },
}

METHODS: dict[str, dict] = {
    "viewer.me": {
        "description": "who is viewing: your name, username and role on this agent",
        "min_role": "viewer",
        "args": {},
    },
    "integrations.status": {
        "description": "which of the connected-account providers this agent uses you have connected",
        "min_role": "viewer",
        "args": {},
    },
    "tasks.run_result": {
        "description": "the result of one task run you may see",
        "min_role": "viewer",
        "args": {"run_id": "string"},
    },
    "notifications.create": {
        "description": "a notification to yourself",
        "min_role": "viewer",
        "args": {"title": "string", "body": "string", "severity": "string"},
    },
    # Files by declared prefixes (APPS.md "Files"): the app's `files` block
    # names what these may reach; the path policy applies after the match.
    "files.list": {
        "description": "the entries of a folder under one of the app's declared read prefixes",
        "min_role": "viewer",
        "args": {"path": "string"},
    },
    "files.read": {
        "description": "a text file under one of the app's declared read prefixes",
        "min_role": "viewer",
        "args": {"path": "string"},
    },
    "files.write": {
        "description": "write a text file under one of the app's declared write prefixes",
        "min_role": "contributor",
        "args": {"path": "string", "content": "string"},
    },
    # An app as the setup page (COMMUNITY-AGENTS-REGISTRY.md "Setup guides"):
    # the viewer's own onboarding state and its completion — the same
    # service the complete_setup tool's route runs. A person's own page
    # only: never a bearer, never the app identity, never a render.
    "setup.status": {
        "description": "whether your setup of this agent, and the agent's own, is still pending, "
                       "and which of the tools the page names are ready for you",
        "min_role": "viewer",
        "args": {},
    },
    "setup.complete": {
        "description": "mark your setup of this agent complete (scope user), or the agent's own as a manager (scope agent)",
        "min_role": "viewer",
        "args": {"scope": "string"},
    },
}
FILE_METHODS = frozenset({"files.list", "files.read", "files.write"})
SETUP_METHODS = frozenset({"setup.status", "setup.complete"})
FILE_MAX_BYTES = 2 * 1024 * 1024
FILE_LIST_MAX = 500

# Sequence numbers per (user, agent, feed), in-process: a gap tells the
# host a delta was dropped; a restart starts over and the host's resync on
# reconnect covers it.
_seq: dict[tuple[str, str, str], int] = {}
_seq_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def install(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Startup: remember the loop (deltas from worker threads hop onto it)
    and register the run-finished hook."""
    global _loop
    _loop = loop or asyncio.get_running_loop()
    from storage.automation import db_tasks
    db_tasks.on_run_finished(_on_run_finished)


def _next_seq(user_sub: str, agent: str, feed: str) -> int:
    key = (user_sub, agent, feed)
    with _seq_lock:
        _seq[key] = _seq.get(key, 0) + 1
        return _seq[key]


def current_seq(user_sub: str, agent: str, feed: str) -> int:
    return _seq.get((user_sub, agent, feed), 0)


def _emit_now(users: Iterable[str], agent: str, feed: str, delta: dict | None,
              snapshot: list | None = None, shared: bool | None = None) -> int:
    """A frame reaches only the connections that subscribed to the feed
    (``catalog_subscribe``); a user with none takes no frame and no
    sequence step, so a tab without such an app costs nothing. An app
    server that subscribed (APPS.md) takes the delta when it lies in the
    agent-scope slice."""
    from services.notifications import notification_manager
    n = 0
    for sub in set(users):
        if not sub or not notification_manager.catalog_subscribed(sub, agent, feed):
            continue
        frame = {"type": wire.CATALOG, "agent": agent, "feed": feed,
                 "seq": _next_seq(sub, agent, feed)}
        if snapshot is not None:
            frame["snapshot"] = snapshot
        else:
            frame["delta"] = delta or {}
        n += notification_manager.push_catalog(sub, agent, feed, frame)
    if delta is not None and snapshot is None:
        n += _push_app_subs(agent, feed, delta, shared)
        # A handler that wakes on this event (APPS.md "Handlers"): a
        # durable delivery, never a frame.
        from services.apps import app_handlers
        app_handlers.on_feed_delta(users, agent, feed, delta, shared)
    return n


# ── App servers as subscribers (APPS.md "Platform methods and feeds") ──────
# An app's server has no user connection: it opens its own socket to the
# proxy and subscribes to feeds of its agent; deltas reach it through a
# live queue of its own, only those in the agent-scope slice (shared
# chats, agent-scope tasks and triggers, workspace files — never a
# member's inbox or personal work).


class AppSubscription:
    """One server socket's feeds and queue: the socket holds it from open
    to close, and only the socket that registered it may take it down."""

    def __init__(self, app_id: str, agent: str, instance: str = "live") -> None:
        from services.notifications.notification_manager import LiveQueue
        self.app_id = app_id
        self.agent = agent
        self.instance = instance
        self.seq_key = f"app:{app_id}:{instance}"
        self.feeds: set[str] = set()
        self.queue = LiveQueue()


# (app id, instance) → the socket serving that process now: the preview
# copy's server and the live one subscribe side by side, and a reconnect
# supersedes the same process's older socket without the older one's close
# taking the newer one down.
_app_subs: dict[tuple[str, str], AppSubscription] = {}
APP_FEEDS_MAX = 16


def open_app_subscription(app_id: str, agent: str, instance: str = "live") -> AppSubscription:
    sub = AppSubscription(app_id, agent, instance)
    _app_subs[(app_id, instance)] = sub
    return sub


def close_app_subscription(sub: AppSubscription) -> None:
    if _app_subs.get((sub.app_id, sub.instance)) is sub:
        _app_subs.pop((sub.app_id, sub.instance), None)


def app_subscribe(sub: AppSubscription, feed: str, on: bool) -> bool:
    if feed not in _SERVER_FEEDS:
        return False
    if on:
        if len(sub.feeds) >= APP_FEEDS_MAX and feed not in sub.feeds:
            return False
        sub.feeds.add(feed)
    else:
        sub.feeds.discard(feed)
    return True


def app_subscription(app_id: str, instance: str = "live") -> AppSubscription | None:
    return _app_subs.get((app_id, instance))


def in_agent_slice(feed: str, delta: dict, shared: bool | None) -> bool:
    """Whether a delta belongs to the agent-scope slice an app identity may
    see. The emit sites say so when they know (``shared``); a session
    delta that does not carry it is looked up (rare: only with an app
    subscribed)."""
    if feed in ("notifications",):
        return False
    if shared is not None:
        return shared
    if feed == "sessions":
        chat = task_store.get_chat(str(delta.get("id") or ""))
        owner = (chat or {}).get("user_sub") or ""
        return is_shared_chat_owner(owner) or is_phone_chat_owner(owner)
    if feed == "file_changes":
        return not layout.is_personal(str(delta.get("rel_path") or ""))
    return False


def _push_app_subs(agent: str, feed: str, delta: dict, shared: bool | None) -> int:
    n = 0
    for sub in list(_app_subs.values()):
        if sub.agent != agent or feed not in sub.feeds:
            continue
        if not in_agent_slice(feed, delta, shared):
            continue
        frame = {"type": wire.CATALOG, "agent": agent, "feed": feed,
                 "seq": _next_seq(sub.seq_key, agent, feed), "delta": delta}
        if sub.queue.put(frame):
            n += 1
    return n


def app_principal(row: dict):
    """The app identity as a principal: a session-shaped caller that acts
    on its own agent and owns nothing of any user (APPS.md "Viewer known
    everywhere, writes on the app identity")."""
    from auth.providers import SESSION_SUB_PREFIX, UserContext
    return UserContext(
        sub=f"{SESSION_SUB_PREFIX}app:{row['id']}", email="app@internal",
        name=row.get("title") or row.get("slug") or "app", role="agent", agents=[],
        is_api_key=True, session_id=f"app:{row['id']}", agent=row.get("agent") or "",
    )


def snapshot_for_app(feed: str, agent: str, row: dict) -> list[dict]:
    """The agent-scope slice of a feed for an app server with no viewer
    behind the call: shared chats, agent-scope tasks and triggers; never
    an inbox."""
    if feed == "sessions":
        from core.session.visibility import SHARED_CHAT_OWNER_PREFIX
        chats = task_store.list_chats(f"{SHARED_CHAT_OWNER_PREFIX}{agent}", agent=agent, limit=100)
        return [session_row(c) for c in chats]
    if feed == "notifications":
        return []
    return snapshot(feed, agent, app_principal(row))


_SERVER_FEEDS = frozenset(FEEDS) - CLIENT_FEEDS
_AGENT_MAX = 64


def handle_subscription(user_sub: str, connection_id: str, msg: dict) -> bool:
    """The dashboard's ``catalog_subscribe`` / ``catalog_unsubscribe`` frame:
    one (agent, feed) pair on the sending connection. Only platform-answered
    feeds are subscribable; the pair is not checked against any app here —
    the frames carry the viewer's own rows, and the host page forwards a
    feed only into a document whose approved manifest declares it."""
    from services.notifications import notification_manager
    feed = str(msg.get("feed") or "")
    agent = str(msg.get("agent") or "")
    if feed not in _SERVER_FEEDS or len(agent) > _AGENT_MAX:
        return False
    on = msg.get("type") == wire.IN_CATALOG_SUBSCRIBE
    return notification_manager.set_catalog_subscription(user_sub, connection_id, agent, feed, on)


def emit(users: Iterable[str], agent: str, feed: str, delta: dict,
         *, shared: bool | None = None) -> None:
    """Push one delta to every listed user's connections (and to the app
    servers subscribed to the agent's feed when it is agent-scope data —
    ``shared``; None lets the slice rule look). Safe from any thread: off
    the loop it hops onto it (the live queue wakes a future)."""
    users = list(users)
    if not users and not _app_subs:
        from services.apps import app_handlers
        if not app_handlers.has_subscribers(agent):
            return
    loop = _loop
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if loop is not None and running is not loop:
        loop.call_soon_threadsafe(_emit_now, users, agent, feed, delta, None, shared)
    else:
        _emit_now(users, agent, feed, delta, None, shared)


# ── Emit sites ──────────────────────────────────────────────────────────────


def _members_and_admins(agent: str) -> list[str]:
    """The agent's members and every admin, from the notification manager's
    audience cache (no store read per emit)."""
    from services.notifications.notification_manager import agent_audience
    return sorted(set(agent_audience(agent)))


def _chat_targets(chat: dict) -> list[str]:
    """Who sees a chat's rows: its owner, or (synthetic owners) the agent's
    users; phone chats belong to the agent's members."""
    from services.notifications.notification_manager import chat_status_targets
    owner = chat.get("user_sub") or ""
    agent = chat.get("agent") or ""
    if is_phone_chat_owner(owner):
        return _members_and_admins(agent)
    return chat_status_targets(owner, agent)


def session_row(chat: dict, status: str = "") -> dict:
    if not status:
        try:
            from services.delegation import lane_status
            status = lane_status.chat_status(chat.get("id") or "")
        except Exception:
            status = "idle"
    return {
        "id": chat.get("id") or "",
        "title": chat.get("title") or "",
        "status": status,
        "project_id": chat.get("project_id") or "",
        "delegate_role": chat.get("delegate_role") or "",
        "source_type": session_kind.of_chat(chat).source_type,
        "updated_at": chat.get("updated_at") or "",
    }


def announce_new_chat(chat: dict | None) -> None:
    """A chat row was created (every ``create_chat`` caller): the ``sessions``
    feed of the agent gains it."""
    if not chat or not chat.get("id"):
        return
    emit(_chat_targets(chat), chat.get("agent") or "", "sessions",
         {**session_row(chat, "idle"), "created": True}, shared=_chat_shared(chat))


def _chat_shared(chat: dict) -> bool:
    owner = chat.get("user_sub") or ""
    return is_shared_chat_owner(owner) or is_phone_chat_owner(owner)


def chat_status_changed(targets: Iterable[str], chat_id: str, status: str, agent: str) -> None:
    """The ``sessions`` twin of ``broadcast_chat_status`` (same targets)."""
    emit(targets, agent, "sessions", {"id": chat_id, "status": status})


def chat_meta_changed(chat_id: str, meta: dict, *, owner: str | None = None,
                      agent: str | None = None) -> None:
    """Project or role stamps on a chat (the orchestrator stamp) and its
    title. A caller that holds the chat's owner and agent passes them and no
    row is read."""
    if owner is None or agent is None:
        chat = task_store.get_chat(chat_id)
        if not chat:
            return
        owner, agent = chat.get("user_sub") or "", chat.get("agent") or ""
    row = {"id": chat_id, "user_sub": owner, "agent": agent}
    emit(_chat_targets(row), agent, "sessions", {"id": chat_id, **meta}, shared=_chat_shared(row))


def notification_delivered(user_sub: str, delivery: dict) -> None:
    emit([user_sub], "", "notifications", {
        "id": delivery.get("id") or "", "title": delivery.get("title") or "",
        "body": delivery.get("body") or "", "severity": delivery.get("severity") or "info",
        "source": delivery.get("source") or "", "delivered_at": delivery.get("delivered_at") or "",
        "read": False, "agent_slug": delivery.get("agent_slug") or "",
        "chat_id": delivery.get("chat_id") or "", "href": delivery.get("href") or "",
    }, shared=False)


def file_changed(user_subs: Iterable[str], agent: str, rel_path: str, *, file_id: str = "",
                 source: str = "") -> None:
    """A workspace file changed; ``file_id`` is set for Collabora saves and
    absent for disk writes (the entry's description says so)."""
    emit(user_subs, agent, "file_changes", {
        "id": rel_path, "rel_path": rel_path, "file_id": file_id or "", "source": source or "",
        "at": datetime.now(timezone.utc).isoformat(),
    }, shared=not layout.is_personal(rel_path))


def trigger_fired(trigger: dict | None, error: str | None) -> None:
    if not trigger:
        return
    agent = trigger.get("agent") or ""
    if (trigger.get("scope") or "agent") == "user":
        users = [trigger.get("created_by") or ""]
    else:
        users = _members_and_admins(agent)
    emit(users, agent, "trigger_fires", {
        "id": trigger.get("id") or "", "name": trigger.get("name") or "",
        "last_fired_at": datetime.now(timezone.utc).isoformat(),
        "fired_count": int(trigger.get("fired_count") or 0) + 1,
        "last_error": error or "",
    }, shared=(trigger.get("scope") or "agent") != "user")


def _on_run_finished(run_id: str, status: str) -> None:
    """``tasks`` delta after a run's terminal write (from the writer's
    thread; the emit hops onto the loop)."""
    try:
        run = task_store.get_run(run_id)
    except Exception:
        return
    if not run:
        return
    agent = run.get("agent") or ""
    if (run.get("scope") or "agent") == "user":
        users = [run.get("created_by") or ""]
    else:
        users = _members_and_admins(agent)
    delta = {
        "id": run.get("task_id") or "",
        # The handlers' dispatch skips an app handler's own run (APPS.md
        # "Handlers"); absent on a legacy row with no type.
        **({"task_type": run["task_type"]} if run.get("task_type") else {}),
        "last_run": {"id": run_id, "status": status,
                     "completed_at": run.get("completed_at") or "",
                     "error_message": run.get("error_message") or ""},
    }
    # The cron just moved a day: the next run rides along. Read from the
    # scheduler's own job table (no DB — this hook also fires on the event
    # loop for a resumed run); a failed lookup leaves the key out rather
    # than claiming "no next run", and never costs the last_run delta.
    try:
        from services.scheduler import scheduler
        delta["next_run_time"] = scheduler.next_run_time(run.get("task_id") or "")
    except Exception:
        logger.debug("tasks feed: next run of %s unknown", run.get("task_id"), exc_info=True)
    emit(users, agent, "tasks", delta, shared=(run.get("scope") or "agent") != "user")


# ── Snapshots (sync, the viewer's slice) ───────────────────────────────────


def _task_visible(task: dict, user) -> bool:
    if (task.get("scope") or "user") == "user":
        return (task.get("created_by") or "") == user.sub or user.is_admin
    return True


def snapshot(feed: str, agent: str, user) -> list[dict]:
    """The viewer's rows of a platform feed for ``agent``. The agent's feeds
    are the members' slice: a viewer a share alone admitted to the app
    (SHARING.md) holds no row on the agent and reads none of its chats,
    tasks, triggers or checks; the inbox is the viewer's own either way."""
    if feed != "notifications" and not user.can_access_agent(agent):
        return []
    if feed == "sessions":
        from core.session.visibility import chat_history_owner
        owner = chat_history_owner(agent, user.sub)
        rows = [session_row(c) for c in task_store.list_chats(owner, agent=agent, limit=100)]
        return rows
    if feed == "tasks":
        # Imported here: the runner and the trigger manager import this
        # module lazily, and the scheduler package imports the runner.
        import config
        from services.scheduler import schedule_text, scheduler
        jobs = {j["task_id"]: j for j in scheduler.get_scheduled_jobs()}
        platform_tz = config.get_platform_timezone()
        out = []
        for t in task_store.list_dynamic_tasks(agent)[:100]:
            if not _task_visible(t, user):
                continue
            runs = task_store.list_runs(limit=1, task_id=t.get("id"))
            last = runs[0] if runs else None
            # The schedule in words, in the task's zone (named when the
            # words carry a clock time), so a page prints text and never
            # humanizes a cron itself — the widget kit prints exactly this.
            effective_tz = t.get("user_tz") or platform_tz
            out.append({
                "id": t.get("id") or "", "name": t.get("name") or "",
                "task_type": t.get("task_type") or "", "schedule": t.get("schedule") or "",
                "interval_seconds": t.get("interval_seconds"), "run_at": t.get("run_at") or "",
                "user_tz": t.get("user_tz") or "", "effective_tz": effective_tz,
                "schedule_text": schedule_text.describe({**t, "effective_tz": effective_tz}),
                "next_run_time": (jobs.get(t.get("id")) or {}).get("next_run_time"),
                "enabled": bool(t.get("enabled", True)), "scope": t.get("scope") or "user",
                "last_run": ({"id": last.get("id"), "status": last.get("status") or "",
                              "completed_at": last.get("completed_at") or "",
                              "error_message": last.get("error_message") or ""}
                             if last else None),
            })
        return out
    if feed == "notifications":
        from storage.automation import notification_store
        return [{
            "id": d.get("id") or "", "title": d.get("title") or "", "body": d.get("body") or "",
            "severity": d.get("severity") or "info", "source": d.get("source") or "",
            "delivered_at": d.get("delivered_at") or "", "read": bool(d.get("read")),
            "agent_slug": d.get("agent_slug") or "", "chat_id": d.get("chat_id") or "",
            "href": d.get("href") or "",
        } for d in notification_store.list_deliveries(user.sub, limit=50)]
    if feed == "trigger_fires":
        from storage.automation import trigger_store
        out = []
        for t in trigger_store.list_triggers(agent=agent):
            if not _task_visible(t, user):
                continue
            out.append({
                "id": t.get("id") or "", "name": t.get("name") or "",
                "last_fired_at": t.get("last_fired_at") or "",
                "fired_count": int(t.get("fired_count") or 0),
                "last_error": t.get("last_error") or "",
            })
        return out
    if feed == "file_changes":
        return []
    if feed == "check_verdicts":
        from storage.checks import db_checks
        scope_sub = None if user.can_manage_agent(agent) else user.sub
        return [verdict_row(v) for v in db_checks.list_verdicts(agent, user_sub=scope_sub, limit=100)]
    if feed == "checks":
        return checks_snapshot(agent, user)
    raise KeyError(feed)


def _viewer_username(agent: str, user) -> str:
    """The viewer's username on an agent with personal sessions; "" on a
    shared-only agent and for an app identity (no private tree)."""
    from core.session.visibility import is_shared_only
    if is_shared_only(agent):
        return ""
    return task_store.get_username_by_sub(getattr(user, "sub", "") or "") or ""


def checks_snapshot(agent: str, user) -> list[dict]:
    """The ``checks`` feed (CHECKS.md): the agent's checks the viewer may
    see — a manager every one, a member those without problems — plus the
    viewer's own private ones, each with the last verdict in the viewer's
    slice (a manager's is the agent's, a member's their own plus the
    agent-scope rows, as ``check_verdicts``)."""
    from services.checks import documents
    from storage.checks import db_checks
    manages = user.can_manage_agent(agent)
    items = [it for it in documents.load_checks(agent, "") if manages or not it.problems]
    username = _viewer_username(agent, user)
    if username:
        items.extend(documents.load_checks(agent, username))
    scope_sub = None if manages else user.sub
    out = []
    for it in items:
        # The listing is by name; a private check may share a name with
        # the agent's, so the owner decides which rows count.
        recent = db_checks.list_verdicts(agent, user_sub=scope_sub, check_name=it.name, limit=10)
        last = next((v for v in recent if (v.get("owner") or "") == it.owner), None)
        out.append(check_row(it, last))
    return out


def check_row(it, last: dict | None) -> dict:
    """A check as the ``checks`` feed carries it: the listing's words, no
    document and no script, and the last verdict trimmed."""
    return {
        "id": it.ref, "name": it.name, "owner": it.owner,
        "description": it.doc.get("description") or "",
        "mandatory": it.mandatory, "applies": it.applies,
        "condition": it.doc.get("condition") or {}, "rounds": it.rounds,
        "sections": it.sections, "broken": bool(it.problems),
        "updated_at": it.updated_at or "",
        "last_verdict": last_verdict_row(last) if last else None,
    }


def last_verdict_row(v: dict) -> dict:
    return {
        "id": v.get("id") or "", "status": v.get("status") or "", "pass": bool(v.get("pass")),
        "score": v.get("score"), "summary": v.get("summary") or "", "round": int(v.get("round") or 0),
        "chat_id": v.get("chat_id") or "", "run_id": v.get("run_id") or "",
        "ran_on": v.get("ran_on") or "", "created_at": v.get("created_at") or "",
    }


def check_changed(agent: str, owner: str, name: str, it, user_sub: str = "") -> None:
    """A check was written (``it``) or removed (None) — the ``checks`` feed
    of the agent's members, or of the owner of a private one. A write
    carries the row without its verdict (the page keeps what it has)."""
    ref = f"user:{name}" if owner else f"agent:{name}"
    users = [user_sub] if owner else _members_and_admins(agent)
    if it is None:
        delta: dict = {"id": ref, "removed": True}
    else:
        delta = {k: v for k, v in check_row(it, None).items() if k != "last_verdict"}
    emit(users, agent, "checks", delta, shared=not owner)


def verdict_row(v: dict) -> dict:
    """A verdict as the feed carries it (CHECKS.md)."""
    return {
        "id": v.get("id") or "", "check": v.get("check_name") or "", "owner": v.get("owner") or "",
        "section": v.get("section") or "", "status": v.get("status") or "",
        "pass": bool(v.get("pass")), "score": v.get("score"), "summary": v.get("summary") or "",
        "findings": (v.get("findings") or [])[:6], "round": int(v.get("round") or 0),
        "chat_id": v.get("chat_id") or "", "run_id": v.get("run_id") or "",
        "ran_on": v.get("ran_on") or "", "cost_usd": float(v.get("cost_usd") or 0),
        "created_at": v.get("created_at") or "",
    }


def verdict_landed(v: dict, agent: str, user_sub: str) -> None:
    """A check's verdict (CHECKS.md): the ``check_verdicts`` feed of the
    person judged, or of the agent's members for an agent-scope session."""
    users = [user_sub] if user_sub else _members_and_admins(agent)
    emit(users, agent, "check_verdicts", verdict_row(v), shared=not user_sub)
    # The ``checks`` feed's row of that check gains the verdict as its last.
    owner = v.get("owner") or ""
    name = v.get("check_name") or ""
    if name:
        emit(users, agent, "checks", {"id": f"user:{name}" if owner else f"agent:{name}",
                                      "last_verdict": last_verdict_row(v)}, shared=not user_sub)


def chat_turn_finished(chat: dict, turn: dict) -> None:
    """A chat's turn ended (CHECKS.md): a ``sessions`` delta carrying
    ``turn`` — the handlers' event ``turn_finished``."""
    if not chat or not chat.get("id"):
        return
    emit(_chat_targets(chat), chat.get("agent") or "", "sessions",
         {"id": chat.get("id") or "", "turn": turn}, shared=_chat_shared(chat))


# ── Methods (sync, as the viewer) ───────────────────────────────────────────


def provider_status(agent: str, row: dict, user) -> list[dict]:
    """``integrations.status``: for each of the agent's connected-account
    MCPs, whether the identity the app's calls RUN WITH has an account —
    the agent's service account for a shared app, the owner's for a
    personal one (APPS.md "Account arguments") — the account's email or
    label, and whose it is: ``you`` (the viewer is that owner), ``the
    owner`` or ``the agent``. The `connect` widget and the approval card
    read it, so nobody is told to connect an account a button never uses
    (the card said "not connected for you" to the agent's own buttons)."""
    from services.apps import headless_exec
    from services.mcp import mcp_registry
    sub, word = headless_exec.runs_as(row)
    if word == "the owner" and sub == (getattr(user, "sub", "") or ""):
        word = "you"
    # A share admits to the app, not to the agent: a viewer who holds no
    # place on it learns whether an account is connected, never whose.
    member = user is not None and user.can_access_agent(agent)
    out = []
    for m in (mcp_registry.get_agent_mcps(agent, placement=placement.LOCAL_PLACEMENT) or []):
        manifest = getattr(m, "manifest", None) or m
        creds = getattr(manifest, "credentials", None)
        oauth = (getattr(creds, "oauth", None) or {}) if creds else {}
        if not oauth:
            continue
        account = headless_exec.connected_account(m.name, agent, sub)
        out.append({"mcp": m.name, "provider": oauth.get("provider_id") or "",
                    "connected": bool(account), "account": account if member else "",
                    "identity": word})
    return out


def run_method(method: str, agent: str, row: dict, user, args) -> dict:
    """Answer one platform method; raises ValueError with a user-facing
    reason for a bad request, PermissionError when the viewer may not."""
    args = args if isinstance(args, dict) else {}
    if method == "viewer.me":
        u = task_store.get_user(user.sub) or {}
        return {
            "sub": user.sub,
            "name": u.get("display_name") or u.get("name") or user.name,
            "username": u.get("username") or "",
            "role": user.acting_role(agent),
            "external": False,
        }
    if method == "integrations.status":
        return {"providers": provider_status(agent, row, user)}
    if method == "tasks.run_result":
        run_id = str(args.get("run_id") or "")
        run = task_store.get_run(run_id) if run_id else None
        if not run or (run.get("agent") or "") != agent:
            raise ValueError("run not found")
        if (run.get("scope") or "agent") == "user" and run.get("created_by") != user.sub \
                and not user.is_admin:
            raise PermissionError("not your run")
        if not user.can_access_agent(agent):
            raise PermissionError("not your agent")
        return {"id": run_id, "status": run.get("status") or "",
                "output_text": (run.get("output_text") or "")[:32_000],
                "error_message": run.get("error_message") or "",
                "started_at": run.get("started_at") or "", "completed_at": run.get("completed_at") or ""}
    if method == "notifications.create":
        title = str(args.get("title") or "").strip()[:200]
        body = str(args.get("body") or "").strip()[:2000]
        severity = str(args.get("severity") or "info")
        if severity not in ("info", "success", "warning", "danger"):
            severity = "info"
        if not title:
            raise ValueError("title is required")
        from storage.automation import notification_store
        if user.is_api_key:
            raise PermissionError("a viewer claim is required to notify someone")
        delivery = notification_store.create_delivery(
            user_sub=user.sub, title=title, body=body, severity=severity, scope="user",
            source="app", agent_slug=agent,
        )
        return {"id": delivery["id"], "delivered": True, "_delivery": delivery}
    if method in FILE_METHODS:
        return _run_file_method(method, agent, row, user, args)
    if method in SETUP_METHODS:
        return _run_setup_method(method, agent, row, user, args)
    raise KeyError(method)


def mcp_status(agent: str, row: dict, user) -> dict[str, dict]:
    """``setup.status``'s ``mcps``: one entry per MCP the page's manifest
    names — ``requires.mcps`` and the ``mcp`` of every ``mcp_tool`` button
    (stored as the server key; answered under the manifest name too) —
    ``{enabled, connected, fix, reason}``. ``enabled``: the agent has it on
    ANY placement (``get_agent_mcps_all_placements`` — the page describes
    the agent's configuration, not the headless placement buttons run on: a
    browser MCP on a satellite is the agent's). ``connected``: this viewer
    has an account when the MCP's credentials are ``per_user`` (any account,
    the Integrations page's own predicate); ``True`` when nothing is to
    connect. ``fix``: ``none`` / ``connect`` (the viewer, under User
    Settings → Integrations) / ``admin`` (not installed, requested and
    waiting, disabled on the platform, explicit mode with no instance
    authorizing the agent, or not enabled for the agent), with a ``reason``
    a page prints as it is — so a setup page greys a card whose MCP is
    missing and says what would fix it."""
    from api.apps import manifest as _mf
    from services.mcp import mcp_registry
    from storage.identity import credential_store
    from storage.mcp import mcp_request_store, mcp_store
    names: list[str] = []
    for n in _mf.parse_requires(row).get("mcps") or []:
        if isinstance(n, str) and n and n not in names:
            names.append(n)
    for a in _mf.parse_actions(row):
        if isinstance(a, dict) and a.get("type") == "mcp_tool":
            key = str(a.get("mcp") or "")
            if key and key not in names:
                names.append(key)
    if not names:
        return {}
    have: dict[str, object] = {}
    for m in mcp_registry.get_agent_mcps_all_placements(agent) or []:
        have[m.name] = m
        if getattr(m, "server_name", ""):
            have[m.server_name] = m
    try:
        open_pairs = mcp_request_store.open_requests_by_pair()
    except Exception:
        logger.exception("setup.status: open MCP requests lookup failed for %s", agent)
        open_pairs = {}
    try:
        states = mcp_store.get_all_mcp_states()
    except Exception:
        states = {}
    viewer_sub = getattr(user, "sub", "") or ""
    out: dict[str, dict] = {}
    for name in names:
        manifest = have.get(name) or mcp_registry.get_manifest(name) \
            or mcp_registry.get_manifest_by_config_key(name)
        canonical = getattr(manifest, "name", "") or name
        label = getattr(manifest, "label", "") or name
        creds = getattr(manifest, "credentials", None)
        per_user = bool(creds) and getattr(creds, "type", "") == "per_user"
        connected = True
        if per_user:
            try:
                connected = bool(credential_store.list_user_accounts(viewer_sub, canonical))
            except Exception:
                logger.exception("setup.status: account lookup failed for %s", canonical)
                connected = False
        enabled = name in have
        if enabled:
            if connected:
                fix, reason = "none", ""
            else:
                fix, reason = "connect", f"connect your {label} account under User Settings → Integrations"
        elif manifest is None:
            fix, reason = "admin", f"{label} is not installed on this OtoDock; ask an admin"
        elif (canonical, agent) in open_pairs:
            fix, reason = "admin", f"{label} is requested for this agent and waits for an admin"
        elif states.get(canonical) is False:
            fix, reason = "admin", f"{label} is disabled on this OtoDock; ask an admin"
        elif getattr(manifest, "assignment_mode", "auto") == "explicit" \
                and not mcp_store.is_agent_authorized_for_mcp(canonical, agent):
            fix, reason = "admin", f"an admin has to attach an instance of {label} to this agent"
        else:
            fix, reason = "admin", f"{label} is not enabled for this agent (Agent Settings → MCPs)"
        entry = {"enabled": enabled, "connected": connected, "fix": fix, "reason": reason}
        out[name] = entry
        if canonical != name:
            out.setdefault(canonical, entry)
    return out


def _run_setup_method(method: str, agent: str, row: dict, user, args: dict) -> dict:
    """The viewer's own setup state — with the agent's name and description
    and the standing of the MCPs the page names (``mcp_status``) — or the
    intent to complete it: the completion itself is async (the delete fans
    out and commits) and runs in ``finish_platform_result`` off the
    ``_setup`` marker. A bearer, the app identity and a render never reach
    a person's setup."""
    from services.agents import setup_state
    from storage.agents import agent_store
    if getattr(user, "is_api_key", False) or getattr(user, "render_app", ""):
        raise PermissionError("a person's own page is required")
    username = task_store.get_username_by_sub(user.sub) or ""
    agent_row = agent_store.get_agent(agent) or {}
    can_manage = bool(user.is_admin or user.can_manage_agent(agent))
    if method == "setup.status":
        return {**setup_state.status(agent, username, agent_row), "can_complete_agent": can_manage,
                "display_name": str(agent_row.get("display_name") or agent),
                "description": str(agent_row.get("description") or ""),
                "mcps": mcp_status(agent, row, user)}
    scope = str(args.get("scope") or "user")
    if scope not in ("user", "agent"):
        raise ValueError("scope is user or agent")
    if scope == "agent" and not can_manage:
        raise PermissionError("completing the agent's own setup needs a manager")
    if scope == "user" and not username:
        raise ValueError("this account has no username")
    return {"scope": scope, "status": "completing",
            "_setup": {"agent": agent, "username": username, "sub": user.sub, "scope": scope}}


# ── Files by declared prefixes (APPS.md "Files") ──────────────────────────


def _declared_prefixes(row: dict, mode: str) -> list[str]:
    """The prefixes the app may reach for ``mode`` (``read`` covers write
    prefixes too); a personal app's ``{owner}`` resolves from the owner's
    row at call time, never from a stored name."""
    from api.apps import manifest as _mf
    block = _mf.parse_files(row)
    prefixes = list(block.get(mode) or [])
    if mode == "read":
        prefixes += [p for p in block.get("write") or [] if p not in prefixes]
    owner = task_store.get_user(row.get("owner_sub") or "") if row.get("username") else None
    owner_name = (owner or {}).get("username") or ""
    out = []
    for p in prefixes:
        if "{owner}" in p:
            if not owner_name:
                continue
            p = p.replace("{owner}", owner_name)
        out.append(p.strip("/"))
    return out


def _matches(rel: str, prefixes: list[str]) -> bool:
    return any(rel == p or rel.startswith(p + "/") for p in prefixes)


def _run_file_method(method: str, agent: str, row: dict, user, args: dict) -> dict:
    """Resolve the path first (the files API's own gate), match the
    declared prefixes against the RESOLVED path, refuse the never-
    declarable segments again, then read, list or write."""
    import config as _config
    from api.agents.files import safe_agent_path
    from services import path_roles
    from services.apps import releases
    from services.apps.app_deploy import NEVER_SEGMENTS
    from fastapi import HTTPException
    # A share admits to the app, not to the agent (SHARING.md): a viewer
    # with no place on the agent reads none of its files, whatever the app
    # declares. The app identity and a render pass as the agent's own.
    if not user.can_access_agent(agent):
        raise PermissionError("not available: the agent's files are for its members, "
                              "and this app was shared with you")
    raw = str(args.get("path") or "").strip().strip("/")
    if not raw or "\x00" in raw:
        raise ValueError("path is required")
    mode = "write" if method == "files.write" else "read"
    prefixes = _declared_prefixes(row, mode)
    if not _matches(raw, prefixes):
        raise PermissionError(f"{raw} is outside the app's declared {mode} prefixes")
    agent_dir = _config.get_agent_dir(agent)
    try:
        resolved, _u = safe_agent_path(agent_dir, agent, raw, user, writing=(mode == "write"))
    except HTTPException as e:
        raise PermissionError(str(e.detail))
    rel = resolved.relative_to(agent_dir.resolve()).as_posix()
    if not _matches(rel, prefixes):
        raise PermissionError(f"{raw} resolves outside the app's declared prefixes")
    parts = rel.split("/")
    if any(seg in NEVER_SEGMENTS for seg in parts) or path_roles.is_protected_credentials_path(rel):
        raise PermissionError("that path is never reachable from an app")
    if method == "files.list":
        if not resolved.is_dir():
            raise ValueError("not a folder")
        out = []
        for child in sorted(resolved.iterdir(), key=lambda p: p.name)[:FILE_LIST_MAX]:
            if child.name.startswith(".") or child.is_symlink():
                continue
            try:
                st = child.stat()
            except OSError:
                continue
            out.append({"name": child.name, "type": "folder" if child.is_dir() else "file",
                        "size": 0 if child.is_dir() else st.st_size})
        return {"path": rel, "entries": out}
    if method == "files.read":
        # The resolved (and so authorized) path is opened strictly: a link
        # swapped in after the check is refused, never followed.
        from services.infra import safe_fs
        try:
            data = safe_fs.read_bytes_beneath(_config.AGENTS_DIR, f"{agent}/{rel}", max_size=FILE_MAX_BYTES)
        except safe_fs.FileTooLarge:
            raise ValueError("the file is larger than 2 MB")
        except FileNotFoundError:
            raise ValueError("not a file")
        except safe_fs.NotRegularFile:
            raise ValueError("not a file")
        except OSError:
            raise PermissionError("that path is not a regular file the app may read")
        return {"path": rel, "content": data.decode("utf-8", "replace"), "size": len(data)}
    content = args.get("content")
    if not isinstance(content, str):
        raise ValueError("content (a string) is required")
    if len(content.encode("utf-8")) > FILE_MAX_BYTES:
        raise ValueError("content is larger than 2 MB")
    if resolved.is_dir():
        raise ValueError("a folder is at that path")
    # Joined from the row's agent and the checked rel, never the realpath:
    # the write lands beneath that agent's folder or nowhere.
    try:
        releases.write_atomic(agent_dir / rel, content)
    except OSError:
        raise PermissionError("that path cannot be written (a link, or not a regular file)")
    return {"path": rel, "size": len(content.encode("utf-8")),
            "_written": (agent, rel, str(resolved))}
