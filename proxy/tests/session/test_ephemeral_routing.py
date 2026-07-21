"""End-of-turn alert emission policy — fire_ephemeral routing + the pump gate.

Pins the decision table row by row:

- **pump path** (defaults): origin-routed — a browser-origin chat pings THAT
  connection and never FCM; an app-origin chat pushes only when backgrounded;
  the no-origin fallthrough keeps the legacy activity rule.
- **interactive** terminals: presence overlay — while any dashboard connection
  is active, the alert takes ``turn_complete`` frames to the active
  connections INSTEAD of the FCM/silent fallthroughs.
- **cli_attached** (otodock terminal owns the session): the recorded origin is
  ignored and only ACTIVE dashboard presence suppresses the push — the CLI
  attachment itself never does. The always-push row is the regression pin.
- **task runs**: the pump's ``source_type == "task"`` never fires the alert
  (completion is the task's ``notification_mode`` contract); a continued
  task chat rides the normal ``"chat"`` pump and keeps it.

Connection/origin state is swapped per-test via monkeypatch; the FCM leg is
stubbed at ``push_sender.send_fcm``.
"""
import asyncio

import pytest

import services.notifications.notification_manager as nm

pytestmark = pytest.mark.asyncio


def _conn(cid, *, active, platform="web", away=False):
    return nm.ConnectionInfo(
        connection_id=cid, queue=asyncio.Queue(), active=active,
        platform=platform, away=away,
    )


def _frames(conn):
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get_nowait())
    return out


@pytest.fixture
def routing(monkeypatch):
    """Fresh connection/origin state + a captured FCM leg."""
    sent_fcm = []

    async def fake_send_fcm(token, payload):
        sent_fcm.append(payload)

    import services.notifications.push_sender as ps
    from storage import notification_store as ns
    monkeypatch.setattr(ps, "send_fcm", fake_send_fcm)
    monkeypatch.setattr(ns, "get_push_subscriptions",
                        lambda u: [{"platform": "android", "subscription_data": "tok"}])
    monkeypatch.setattr(nm, "_install_id", lambda: "INST")
    monkeypatch.setattr(nm, "_user_connections", {})
    monkeypatch.setattr(nm, "_chat_turn_origin", {})
    return {"fcm": sent_fcm}


# --- pump path (defaults) — origin routing, unchanged -----------------------


async def test_pump_browser_origin_pings_that_conn_only(routing):
    # active=False, away=False = a HIDDEN origin tab: frame only, never FCM
    # (same-machine multitasking must not buzz the phone).
    c_origin = _conn("c1", active=False)
    c_other = _conn("c2", active=True)
    nm._user_connections["u"] = [c_origin, c_other]
    nm._chat_turn_origin["chat-1"] = ("c1", "web")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1")
    assert [f["type"] for f in _frames(c_origin)] == ["turn_complete"]
    assert _frames(c_other) == []
    assert routing["fcm"] == []


async def test_pump_browser_origin_away_pushes_too(routing):
    # Dashboard visibly open but input-idle: the frame still plays the local
    # toast/sound AND the FCM leg runs — the user stepped away from the screen.
    c_origin = _conn("c1", active=False, away=True)
    nm._user_connections["u"] = [c_origin]
    nm._chat_turn_origin["chat-1"] = ("c1", "web")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1")
    assert [f["type"] for f in _frames(c_origin)] == ["turn_complete"]
    assert len(routing["fcm"]) == 1


async def test_pump_browser_origin_away_defers_to_other_active(routing):
    # Away origin + a genuinely active connection elsewhere: the engaged
    # device covers it — origin frame only, no push.
    c_origin = _conn("c1", active=False, away=True)
    c_other = _conn("c2", active=True)
    nm._user_connections["u"] = [c_origin, c_other]
    nm._chat_turn_origin["chat-1"] = ("c1", "web")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1")
    assert [f["type"] for f in _frames(c_origin)] == ["turn_complete"]
    assert _frames(c_other) == []
    assert routing["fcm"] == []


async def test_pump_browser_origin_gone_drops(routing):
    # A browser chat never buzzes the phone, even with another tab active.
    c_other = _conn("c2", active=True)
    nm._user_connections["u"] = [c_other]
    nm._chat_turn_origin["chat-1"] = ("c1", "web")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1")
    assert _frames(c_other) == []
    assert routing["fcm"] == []


async def test_pump_android_origin_foreground_silent(routing):
    c_app = _conn("c1", active=True, platform="android")
    nm._user_connections["u"] = [c_app]
    nm._chat_turn_origin["chat-1"] = ("c1", "android")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1")
    assert _frames(c_app) == []
    assert routing["fcm"] == []


async def test_pump_android_origin_backgrounded_pushes(routing):
    # The pump path stays origin-routed: an app-origin chat pushes when the
    # app is backgrounded, even while a browser tab is active elsewhere.
    c_app = _conn("c1", active=False, platform="android")
    c_web = _conn("c2", active=True)
    nm._user_connections["u"] = [c_app, c_web]
    nm._chat_turn_origin["chat-1"] = ("c1", "android")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1")
    assert _frames(c_web) == []
    assert len(routing["fcm"]) == 1


async def test_pump_no_origin_active_conn_silent(routing):
    c = _conn("c1", active=True)
    nm._user_connections["u"] = [c]
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1")
    assert _frames(c) == []
    assert routing["fcm"] == []


async def test_pump_no_origin_no_active_pushes(routing):
    nm._user_connections["u"] = [_conn("c1", active=False)]
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1")
    assert len(routing["fcm"]) == 1
    assert routing["fcm"][0]["ephemeral"] is True


# --- interactive presence overlay --------------------------------------------


async def test_interactive_no_origin_active_conns_get_frames(routing):
    # Where the pump path stays silent, an interactive turn end reaches the
    # engaged dashboard as turn_complete frames on every active connection.
    c_active1 = _conn("c1", active=True)
    c_active2 = _conn("c2", active=True)
    c_idle = _conn("c3", active=False)
    nm._user_connections["u"] = [c_active1, c_active2, c_idle]
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1", interactive=True)
    assert [f["chat_id"] for f in _frames(c_active1)] == ["chat-1"]
    assert [f["chat_id"] for f in _frames(c_active2)] == ["chat-1"]
    assert _frames(c_idle) == []
    assert routing["fcm"] == []


async def test_interactive_android_origin_backgrounded_prefers_dashboard(routing):
    # Backgrounded app origin + a focused browser: the in-app path wins.
    c_app = _conn("c1", active=False, platform="android")
    c_web = _conn("c2", active=True)
    nm._user_connections["u"] = [c_app, c_web]
    nm._chat_turn_origin["chat-1"] = ("c1", "android")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1", interactive=True)
    assert [f["type"] for f in _frames(c_web)] == ["turn_complete"]
    assert routing["fcm"] == []


async def test_interactive_browser_origin_still_pings_origin(routing):
    c_origin = _conn("c1", active=False)
    c_active = _conn("c2", active=True)
    nm._user_connections["u"] = [c_origin, c_active]
    nm._chat_turn_origin["chat-1"] = ("c1", "web")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1", interactive=True)
    assert [f["type"] for f in _frames(c_origin)] == ["turn_complete"]
    assert _frames(c_active) == []
    assert routing["fcm"] == []


async def test_interactive_no_presence_pushes(routing):
    # No active presence → FCM; the idle-but-open web dashboard still gets the
    # frame so it toasts alongside the push (the user may be looking).
    c_idle = _conn("c1", active=False)
    nm._user_connections["u"] = [c_idle]
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1", interactive=True)
    assert len(routing["fcm"]) == 1
    assert [f["type"] for f in _frames(c_idle)] == ["turn_complete"]


async def test_interactive_browser_origin_away_frames_once_and_pushes(routing):
    # Interactive turn, away browser origin, everything else idle: the origin
    # gets exactly ONE frame (not re-framed by the idle-web loop), other idle
    # web conns get theirs, android stays FCM-only — and the push fires.
    c_origin = _conn("c1", active=False, away=True)
    c_idle = _conn("c2", active=False)
    c_app = _conn("c3", active=False, platform="android")
    nm._user_connections["u"] = [c_origin, c_idle, c_app]
    nm._chat_turn_origin["chat-1"] = ("c1", "web")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1", interactive=True)
    assert [f["type"] for f in _frames(c_origin)] == ["turn_complete"]
    assert [f["type"] for f in _frames(c_idle)] == ["turn_complete"]
    assert _frames(c_app) == []
    assert len(routing["fcm"]) == 1


async def test_interactive_browser_origin_away_prefers_other_active(routing):
    # Away origin but someone is genuinely active on another device: frames to
    # the origin + the active conns, no push.
    c_origin = _conn("c1", active=False, away=True)
    c_active = _conn("c2", active=True)
    nm._user_connections["u"] = [c_origin, c_active]
    nm._chat_turn_origin["chat-1"] = ("c1", "web")
    await nm.fire_ephemeral("u", "T", "B", chat_id="chat-1", interactive=True)
    assert [f["type"] for f in _frames(c_origin)] == ["turn_complete"]
    assert [f["type"] for f in _frames(c_active)] == ["turn_complete"]
    assert routing["fcm"] == []


async def test_set_connection_active_away_lifecycle():
    q: asyncio.Queue = asyncio.Queue()
    nm._user_connections["u2"] = [nm.ConnectionInfo(connection_id="c1", queue=q)]
    nm.set_connection_active("u2", "c1", False, away=True)
    conn = nm.get_connection("u2", "c1")
    assert (conn.active, conn.away) == (False, True)
    nm.set_connection_active("u2", "c1", True)
    assert (conn.active, conn.away) == (True, False)
    # away can never ride an active connection
    nm.set_connection_active("u2", "c1", True, away=True)
    assert (conn.active, conn.away) == (True, False)
    nm._user_connections.pop("u2", None)


# --- otodock CLI attachment — always-push regression pins --------------------


async def test_cli_attached_always_pushes_without_active_presence(routing):
    # THE regression pin: an open otodock terminal, a stale viewer origin from
    # before the takeover, and idle (connected-but-inactive) dashboard
    # connections — the phone push MUST still fire. A CLI attachment is not
    # dashboard presence, and the stale origin must never swallow the push.
    c_stale = _conn("c1", active=False)  # the superseded viewer, still connected
    nm._user_connections["u"] = [c_stale]
    nm._chat_turn_origin["chat-1"] = ("c1", "web")
    await nm.fire_ephemeral(
        "u", "T", "B", chat_id="chat-1", interactive=True, cli_attached=True,
    )
    assert _frames(c_stale) == []
    assert len(routing["fcm"]) == 1


async def test_cli_attached_no_connections_pushes(routing):
    await nm.fire_ephemeral(
        "u", "T", "B", chat_id="chat-1", interactive=True, cli_attached=True,
    )
    assert len(routing["fcm"]) == 1


async def test_cli_attached_active_dashboard_takes_in_app_path(routing):
    # Dashboard presence (a genuinely active client) is the ONE thing that
    # suppresses the CLI-attached push — the frames fire instead.
    c_active = _conn("c1", active=True)
    nm._user_connections["u"] = [c_active]
    await nm.fire_ephemeral(
        "u", "T", "B", chat_id="chat-1", interactive=True, cli_attached=True,
    )
    assert [f["type"] for f in _frames(c_active)] == ["turn_complete"]
    assert routing["fcm"] == []


# --- pump emission gate — task runs -------------------------------------------


def _pump(source_type):
    from core.events.stream_pump import ChatStreamPump
    return ChatStreamPump(
        chat_id="task-42", session_id="sid-1", producer=None,
        event_queue=asyncio.Queue(), perm_queue=None, source_type=source_type,
    )


@pytest.fixture
def pump_end(monkeypatch):
    """Stub the pump's end-of-turn collaborators; returns the recorded calls."""
    calls = {"ready": [], "ephemeral": []}

    async def fake_fire_ephemeral(user_sub, title, body, chat_id=None, **kw):
        calls["ephemeral"].append((user_sub, chat_id))

    from core.events import stream_pump as sp
    monkeypatch.setattr(sp.task_store, "get_chat",
                        lambda cid: {"user_sub": "u", "agent": "researcher"})
    monkeypatch.setattr(sp.task_store, "get_active_meeting_for_chat", lambda cid: None)
    monkeypatch.setattr(sp.notification_manager, "broadcast_chat_status",
                        lambda u, c, s, agent="": calls["ready"].append((c, s)))
    monkeypatch.setattr(sp.notification_manager, "fire_ephemeral", fake_fire_ephemeral)
    return calls


# --- chat_status fan-out — shared-only chats route to the agent's users -------


async def test_chat_status_targets_plain_owner():
    from services.notifications import notification_manager as nm
    assert nm.chat_status_targets("user-1", "researcher") == ["user-1"]
    assert nm.chat_status_targets("", "researcher") == []


async def test_chat_status_targets_shared_owner(monkeypatch):
    # The synthetic agent:: owner has no connections of its own — the fan-out
    # resolves to every user of the agent (same set the notification system
    # uses for scope="agent").
    from services.notifications import notification_manager as nm
    monkeypatch.setattr(nm.notification_store, "get_agent_user_subs",
                        lambda agent: ["alice", "bob"] if agent == "so" else [])
    assert nm.chat_status_targets("agent::so", "so") == ["alice", "bob"]
    assert nm.chat_status_targets("agent::so", "") == []


async def test_task_pump_broadcasts_ready_but_never_pings(pump_end):
    _pump("task")._fire_end_of_turn()
    await asyncio.sleep(0)
    assert pump_end["ready"] == [("task-42", "ready")]
    assert pump_end["ephemeral"] == []


async def test_continued_task_chat_pump_keeps_ping(pump_end):
    # A re-warmed task chat runs through the dashboard pump (source_type
    # "chat") — its follow-up turns keep the end-of-turn ping.
    _pump("chat")._fire_end_of_turn()
    await asyncio.sleep(0)
    assert pump_end["ready"] == [("task-42", "ready")]
    assert pump_end["ephemeral"] == [("u", "task-42")]
