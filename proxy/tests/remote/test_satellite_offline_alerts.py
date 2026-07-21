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
    monkeypatch.setattr(
        remote_status, "get_live_machine_status",
        lambda mid: state["status"][mid],
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
