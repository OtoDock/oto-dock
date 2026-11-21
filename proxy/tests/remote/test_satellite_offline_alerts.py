"""Tests for the sustained-outage admin notification evaluator.

`SatelliteConnectionManager._evaluate_admin_machine_alerts` replaced the
old connect/disconnect-edge notifications + 30s flap debounce. It fires an
admin "offline" notice only after an admin-paired machine has been
unreachable past the grace window, and a "back online" notice only when a
previously-alerted machine recovers. Everything is driven off persisted
state (`last_seen` + `offline_alerted`) so it's restart-safe and doesn't
spam on proxy restarts / satellite auto-updates / brief blips.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core.remote import satellite_connection as sc
from core.remote.satellite_connection import SatelliteConnectionManager


def _iso_ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


@pytest.fixture
def harness(monkeypatch):
    """Patch the evaluator's three external dependencies and capture calls.

    Returns a dict with:
      - set_machines(list): set what get_all_remote_machines returns
      - set_status(fn): machine_id -> live-status dict
      - alerted_calls: list[(machine_id, bool)] from set_offline_alerted
      - notif_calls: list[(machine_id, online_bool)] from the fan-out fn
    """
    state = {
        "machines": [],
        "status": {},
        "alerted_calls": [],
        "notif_calls": [],
    }

    from storage import remote_store

    monkeypatch.setattr(
        remote_store, "get_all_remote_machines",
        lambda: list(state["machines"]),
    )

    def _set_alerted(machine_id, alerted):
        state["alerted_calls"].append((machine_id, alerted))
        # Reflect into the in-memory machine rows so a follow-up tick in the
        # same test sees the persisted flag flip.
        for m in state["machines"]:
            if m["id"] == machine_id:
                m["offline_alerted"] = alerted
    monkeypatch.setattr(remote_store, "set_offline_alerted", _set_alerted)

    from services.remote import remote_status
    # The evaluator passes the row it already holds (``machine=``) so it
    # never re-reads the DB on the loop — accept and ignore it here.
    monkeypatch.setattr(
        remote_status, "get_live_machine_status",
        lambda mid, **_kw: state["status"][mid],
    )

    async def _fake_notify(machine_id, *, online):
        state["notif_calls"].append((machine_id, online))
    monkeypatch.setattr(sc, "_notify_admins_machine_state_change", _fake_notify)

    state["set_machines"] = lambda ms: state.__setitem__("machines", ms)
    state["set_status"] = lambda d: state.__setitem__("status", d)
    return state


def _machine(mid="m-admin", scope="admin", alerted=False, last_seen=None):
    return {
        "id": mid,
        "name": mid,
        "pairing_scope": scope,
        "offline_alerted": alerted,
        "last_seen": last_seen if last_seen is not None else _iso_ago(10),
    }


def _live(state, age=None):
    return {
        "state": state,
        "last_heartbeat_age_s": age,
        "last_seen_iso": "",
        "reachable": state in ("online", "stale"),
    }


@pytest.mark.asyncio
async def test_healthy_machine_no_notification(harness):
    harness["set_machines"]([_machine()])
    harness["set_status"]({"m-admin": _live("online", age=5)})
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == []
    assert harness["alerted_calls"] == []


@pytest.mark.asyncio
async def test_brief_outage_within_grace_is_silent(harness):
    # Disconnected, but only down ~30s — well under the 120s grace. This is
    # the proxy-restart / auto-update case: no notification.
    harness["set_machines"]([_machine(last_seen=_iso_ago(30))])
    harness["set_status"]({"m-admin": _live("disconnected")})
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == []
    assert harness["alerted_calls"] == []


@pytest.mark.asyncio
async def test_sustained_outage_fires_offline_once(harness):
    # Down longer than the grace window and not yet alerted → fire offline
    # and set the persisted flag exactly once.
    harness["set_machines"](
        [_machine(last_seen=_iso_ago(sc._OFFLINE_ALERT_GRACE_S + 60))]
    )
    harness["set_status"]({"m-admin": _live("disconnected")})
    cm = SatelliteConnectionManager()
    await cm._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == [("m-admin", False)]
    assert harness["alerted_calls"] == [("m-admin", True)]

    # A second tick while still down must NOT re-fire (flag now persisted).
    harness["notif_calls"].clear()
    harness["alerted_calls"].clear()
    await cm._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == []
    assert harness["alerted_calls"] == []


@pytest.mark.asyncio
async def test_recovery_clears_flag_and_fires_online(harness):
    harness["set_machines"]([_machine(alerted=True, last_seen=_iso_ago(5))])
    harness["set_status"]({"m-admin": _live("online", age=2)})
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == [("m-admin", True)]
    assert harness["alerted_calls"] == [("m-admin", False)]


@pytest.mark.asyncio
async def test_online_only_fires_if_previously_alerted(harness):
    # Reachable but never alerted (a transient drop we never reported) →
    # no "back online" spam.
    harness["set_machines"]([_machine(alerted=False)])
    harness["set_status"]({"m-admin": _live("online", age=2)})
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == []


@pytest.mark.asyncio
async def test_user_paired_machine_never_pages_admins(harness):
    # A user-paired machine down for hours must not page admins.
    harness["set_machines"](
        [_machine(mid="m-user", scope="user", last_seen=_iso_ago(99999))]
    )
    harness["set_status"]({"m-user": _live("disconnected")})
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == []
    assert harness["alerted_calls"] == []


@pytest.mark.asyncio
async def test_never_connected_machine_is_silent(harness):
    # Paired but never online → nothing to alert about.
    harness["set_machines"]([_machine(last_seen=None)])
    harness["set_machines"](
        [{"id": "m-admin", "name": "m-admin", "pairing_scope": "admin",
          "offline_alerted": False, "last_seen": None}]
    )
    harness["set_status"]({"m-admin": _live("never_connected")})
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == []
    assert harness["alerted_calls"] == []


# ---------------------------------------------------------------------------
# A compute machine: nothing of the platform's place runs without one, so
# admins are told once when it stays away and once when it is back.
# ---------------------------------------------------------------------------

def _compute(mid="compute-1", alerted=False, last_seen=None):
    return {"id": mid, "name": "Compute", "pairing_scope": "compute",
            "offline_alerted": alerted, "last_seen": last_seen}


def _watched_for(monkeypatch, seconds: float) -> None:
    """This process has been watching for ``seconds``."""
    import time

    from core.remote import satellite_admin_alerts
    monkeypatch.setattr(satellite_admin_alerts, "_watching_since", time.monotonic() - seconds)


@pytest.mark.asyncio
async def test_a_compute_machine_away_past_the_grace_is_reported_once_and_once_back(harness, monkeypatch):
    _watched_for(monkeypatch, 3600)
    harness["set_machines"]([_compute(last_seen=_iso_ago(sc._OFFLINE_ALERT_GRACE_S + 30))])
    harness["set_status"]({"compute-1": _live("disconnected")})
    manager = SatelliteConnectionManager()
    await manager._evaluate_admin_machine_alerts()
    await manager._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == [("compute-1", False)]
    assert harness["alerted_calls"] == [("compute-1", True)]
    harness["set_status"]({"compute-1": _live("online", age=2)})
    await manager._evaluate_admin_machine_alerts()
    await manager._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == [("compute-1", False), ("compute-1", True)]


@pytest.mark.asyncio
async def test_a_restart_of_the_platform_never_reports_a_compute_machine_about_to_sign_in(harness, monkeypatch):
    """After any restart of the platform a compute machine's last contact
    is old (it was away for the whole stop). Its downtime counts from this
    process's start, so it is not reported before the grace has passed
    since then."""
    _watched_for(monkeypatch, 20)
    harness["set_machines"]([_compute(last_seen=_iso_ago(3 * 3600))])
    harness["set_status"]({"compute-1": _live("disconnected")})
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == [] and harness["alerted_calls"] == []
    _watched_for(monkeypatch, sc._OFFLINE_ALERT_GRACE_S + 5)
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == [("compute-1", False)]


@pytest.mark.asyncio
async def test_the_installs_own_compute_machine_that_never_signed_in_is_reported(harness, monkeypatch):
    """A compute service that never came up (after an install, a restore, a
    failed sandbox check) has no last contact at all. The install's own is
    reported once the grace has passed since the start. An added compute
    server that never connected is not: nobody waits for it."""
    from core.remote import satellite_admin_alerts
    monkeypatch.setattr(satellite_admin_alerts, "_own_compute_id", lambda: "compute-own")
    harness["set_machines"]([_compute("compute-own"), _compute("compute-added")])
    harness["set_status"]({"compute-own": _live("never_connected"),
                           "compute-added": _live("never_connected")})
    _watched_for(monkeypatch, 30)
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == []
    _watched_for(monkeypatch, sc._OFFLINE_ALERT_GRACE_S + 5)
    await SatelliteConnectionManager()._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == [("compute-own", False)]
    assert harness["alerted_calls"] == [("compute-own", True)]


@pytest.mark.asyncio
async def test_a_planned_stop_of_the_compute_service_pages_nobody(harness, monkeypatch):
    """A backup or an update holds new work and stops the compute service on
    purpose: while the platform holds, a compute machine that is away is not
    reported, however long the copy takes. One still away when work comes
    back is reported then. An admin's own machine is reported as always."""
    from core import drain
    _watched_for(monkeypatch, 3600)
    harness["set_machines"]([_compute(last_seen=_iso_ago(sc._OFFLINE_ALERT_GRACE_S + 600)),
                             _machine(last_seen=_iso_ago(sc._OFFLINE_ALERT_GRACE_S + 600))])
    harness["set_status"]({"compute-1": _live("disconnected"), "m-admin": _live("disconnected")})
    monkeypatch.setattr(drain, "holding", lambda: True)
    manager = SatelliteConnectionManager()
    await manager._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == [("m-admin", False)]
    monkeypatch.setattr(drain, "holding", lambda: False)
    await manager._evaluate_admin_machine_alerts()
    assert harness["notif_calls"] == [("m-admin", False), ("compute-1", False)]


@pytest.mark.asyncio
async def test_the_compute_notices_say_what_stops_and_what_runs_again(monkeypatch):
    """The two texts admins read: a compute machine's own, never the paired
    machine's wording, and nothing for a person's own computer."""
    from services.notifications import notification_manager
    from storage import remote_store
    rows = {"compute-1": {"id": "compute-1", "name": "Compute", "pairing_scope": "compute"},
            "m-user": {"id": "m-user", "name": "Laptop", "pairing_scope": "user"}}
    monkeypatch.setattr(remote_store, "get_remote_machine", rows.get)
    monkeypatch.setattr(sc, "_list_admin_subs", lambda: ["admin-1"])
    sent: list = []

    async def _fire(**kw):
        sent.append(kw)
    monkeypatch.setattr(notification_manager, "fire_notification", _fire)
    await sc._notify_admins_machine_state_change("compute-1", online=False)
    await sc._notify_admins_machine_state_change("compute-1", online=True)
    await sc._notify_admins_machine_state_change("m-user", online=False)
    texts = [(kw.get("title"), kw.get("body"), kw.get("severity")) for kw in sent]
    assert [t[0] for t in texts] == ["Compute machine offline: Compute", "Compute machine online: Compute"]
    assert texts[0][1] == ("The compute machine 'Compute' is not connected. Chats, tasks, calls and apps "
                           "wait or are refused until it is back. Its log says why.") and texts[0][2] == "warning"
    assert "is connected again" in texts[1][1] and texts[1][2] == "info"


def test_seconds_since_iso_handles_naive_and_aware_and_garbage():
    assert sc._seconds_since_iso(None) is None
    assert sc._seconds_since_iso("not-a-date") is None
    # ~100s ago, aware
    aware = sc._seconds_since_iso(_iso_ago(100))
    assert aware is not None and 95 <= aware <= 110
    # naive treated as UTC
    naive = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=50)
    val = sc._seconds_since_iso(naive.isoformat())
    assert val is not None and 45 <= val <= 60
    # future timestamp clamps to 0 (clock skew)
    assert sc._seconds_since_iso(_iso_ago(-500)) == 0.0
