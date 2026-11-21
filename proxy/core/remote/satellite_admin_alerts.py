"""Admin offline/online machine-state alerting for satellites (mixin).

Fires admin notifications when a satellite's connectivity changes in a SUSTAINED
way (debounced past a grace window), so a transient blip doesn't page admins.
Mixed into SatelliteConnectionManager; split out of satellite_connection.py.
The notifier (`_notify_admins_machine_state_change`), `_seconds_since_iso`, and
`_OFFLINE_ALERT_GRACE_S` stay in satellite_connection (monkeypatched / read by
tests there) and are imported lazily.
"""

import logging
import time
from typing import TYPE_CHECKING

from core import drain, placement

logger = logging.getLogger("otodock.satellite")

# When this process started watching: a compute machine's downtime never
# counts from before it (its ``last_seen`` is old after any restart of the
# platform, and the install's own that never signed in has none).
_watching_since = time.monotonic()


if TYPE_CHECKING:
    from core.remote.satellite_host import SatelliteManagerHost as _Host
else:
    _Host = object


def _own_compute_id() -> str:
    """The install's own compute machine's id, "" when it runs none."""
    from services.remote import compute_bootstrap
    return compute_bootstrap.own_machine_id() if compute_bootstrap.runs_own() else ""


class SatelliteAdminAlertsMixin(_Host):
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

        A compute machine is watched the same way, with its own two texts:
        nothing of the platform's place runs without one. Its downtime is
        the shorter of the time since its last contact and the time this
        process has run, so a restart of the platform never reports a
        machine that is about to sign in, and the install's own machine
        that never signed in (after an install, a restore, a failed sandbox
        check) is reported once the grace has passed since the start. While
        the platform holds new work (``drain.holding``: an update, a backup,
        the start's own wait) a compute machine that is away is not reported.

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
            _OFFLINE_ALERT_GRACE_S,
            _notify_admins_machine_state_change,
            _seconds_since_iso,
        )
        from services.remote import remote_status
        from storage import remote_store
        from storage.pg import run_db

        machines = await run_db(remote_store.get_all_remote_machines)
        own_compute = await run_db(_own_compute_id)
        for m in machines:
            compute = placement.machine_is_compute(m)
            if not (compute or placement.machine_is_admin_paired(m)):
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
            never = live["state"] == remote_status.NEVER_CONNECTED
            if alerted or (never and machine_id != own_compute):
                continue
            if compute and drain.holding():
                # The platform holds new work: an update or a backup the admin
                # started stopped the compute service, or the start still
                # waits for it. Nobody is paged for a stop that was asked for;
                # one still away when work comes back is reported then.
                continue
            downtime = _seconds_since_iso(m.get("last_seen"))
            if compute:
                watched = time.monotonic() - _watching_since
                downtime = watched if downtime is None else min(downtime, watched)
            if downtime is not None and downtime > _OFFLINE_ALERT_GRACE_S:
                await run_db(remote_store.set_offline_alerted, machine_id, True)
                await _notify_admins_machine_state_change(
                    machine_id, online=False
                )
