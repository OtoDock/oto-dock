"""Admin offline/online machine-state alerting for satellites (mixin).

Fires admin notifications when a satellite's connectivity changes in a SUSTAINED
way (debounced past a grace window), so a transient blip doesn't page admins.
Mixed into SatelliteConnectionManager; split out of satellite_connection.py.
The notifier (`_notify_admins_machine_state_change`), `_seconds_since_iso`, and
`_OFFLINE_ALERT_GRACE_S` stay in satellite_connection (monkeypatched / read by
tests there) and are imported lazily.
"""

import logging
from core import placement

logger = logging.getLogger("claude-proxy.satellite")


class SatelliteAdminAlertsMixin:
    async def _evaluate_admin_machine_alerts(self) -> None:
        """Fire admin offline/online notifications based on SUSTAINED state.

        Called once per heartbeat-monitor tick. Walks every admin-paired
        machine and reconciles its live reachability against the persisted
        ``offline_alerted`` flag:

        - **Reachable + alerted** → the machine recovered from an outage we
          told admins about → clear the flag and fire the "back online"
          notice.
        - **Unreachable + not alerted + down longer than the grace window**
          → genuine sustained outage / failed reconnect → set the flag and
          fire the "offline" notice (once).
        - Everything else (reachable-and-healthy, briefly-down,
          already-alerted-and-still-down, never-connected) → no
          notification.

        Downtime is measured from ``last_seen`` (refreshed on every
        heartbeat), so a machine that disconnects starts its grace clock at
        the last contact. User-paired machines are skipped entirely — their
        owner sees the soft-fallback banner, admins are not paged.

        This replaces the old connect/disconnect-edge notifications +
        cancel-on-flip debounce. Because it reads only persisted state it
        cannot be fooled by a proxy restart: a healthy machine that drops
        and reconnects within grace is never reported, while one that was
        already alerted before the restart still resolves correctly on
        reconnect.
        """
        # These stay in satellite_connection (monkeypatched / read by tests
        # there) — import live each call so the patches take effect.
        from core.remote.satellite_connection import (
            _notify_admins_machine_state_change,
            _seconds_since_iso,
            _OFFLINE_ALERT_GRACE_S,
        )
        from storage import remote_store
        from storage.pg import run_db
        from services.remote import remote_status

        machines = await run_db(remote_store.get_all_remote_machines)
        for m in machines:
            if not placement.machine_is_admin_paired(m):
                continue
            # A deliberately paused machine (tray Pause) is offline by
            # intent — skip it so we never page admins. The flag clears on
            # the next successful auth, restoring normal monitoring.
            if m.get("paused"):
                continue
            machine_id = m["id"]
            alerted = bool(m.get("offline_alerted"))
            # Pass the row we already hold: the evaluator runs on the loop
            # every 30 s and must not re-read every machine from the DB.
            live = remote_status.get_live_machine_status(machine_id, machine=m)

            if live["reachable"]:
                if alerted:
                    await run_db(remote_store.set_offline_alerted, machine_id, False)
                    await _notify_admins_machine_state_change(
                        machine_id, online=True
                    )
                continue

            # Not reachable. Alert only once, and only past the grace window.
            if alerted or live["state"] == remote_status.NEVER_CONNECTED:
                continue
            downtime = _seconds_since_iso(m.get("last_seen"))
            if downtime is not None and downtime > _OFFLINE_ALERT_GRACE_S:
                await run_db(remote_store.set_offline_alerted, machine_id, True)
                await _notify_admins_machine_state_change(
                    machine_id, online=False
                )
