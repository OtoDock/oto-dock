"""Satellite-side policy cache (defense-in-depth).

The proxy is the policy authority for path admission — `path_policy_v2`
on the proxy decides whether a given LLM-supplied path is in scope for
the session's role / target / `allow_full_fs` setting. But once a write
is admitted, the satellite trusts the proxy's command stream verbatim.

This module gives the satellite its OWN copy of the security-critical
flags so it can re-validate `PathRef("satellite_host")` writes locally.
If the proxy is compromised and tries to make a home-only satellite
write to `/etc/sudoers`, the satellite rejects based on its own copy of
`allow_full_fs` even though the proxy admitted the call (``is_full_fs_allowed``
is read by ``session_manager``'s satellite-host path re-check).

The policy lands here via two paths:

  * **At WS auth time** — the proxy includes a ``policy`` block in the
    ``auth_result`` response (see ``proxy/ws/satellite.py``). The
    satellite calls ``set_policy(...)`` on receipt.
  * **Mid-session** — admin/user toggling ``allow_full_fs`` from the
    dashboard fires a ``policy_update`` WS message which the satellite
    handles in its message loop (``ws_client.py``).

The ``policy`` block also carries ``device_grants``: the device-control
capabilities (computer / browser / app) the owner has granted this
machine. Device-MCP
enforcement is entirely PROXY-side — an ungranted device MCP is never built
into the config sent here, so it never attaches. ``set_policy`` caches the
grants the proxy sends, but there is currently no satellite-side consumer;
the value is retained for a possible future tool-time re-check.

Reads go through ``get_policy()`` (a copy of the latest applied state).
Defaults ``allow_full_fs = False`` (home-only) and ``device_grants = []`` are
the safer baseline if the proxy never sent a policy block — e.g. an older
proxy version connecting to a new satellite.
"""

from __future__ import annotations

import threading
from typing import TypedDict


class _Policy(TypedDict, total=False):
    allow_full_fs: bool
    device_grants: list[str]
    sync_max_file_bytes: int
    sync_ignore_rules: dict


_lock = threading.Lock()
_state: _Policy = {"allow_full_fs": False, "device_grants": []}


def set_policy(policy: _Policy) -> None:
    """Replace the cached policy. Called on auth_result + policy_update.

    Missing keys retain their previous value — the proxy can send a
    partial update for a single field without nuking the rest.
    """
    if not isinstance(policy, dict):
        return
    with _lock:
        if "allow_full_fs" in policy:
            _state["allow_full_fs"] = bool(policy["allow_full_fs"])
        if "device_grants" in policy:
            raw = policy["device_grants"]
            _state["device_grants"] = (
                sorted({str(x) for x in raw}) if isinstance(raw, list) else []
            )
        if "sync_max_file_bytes" in policy:
            # Universal per-file sync cap from the proxy (0.5.103+ handshake).
            # Absent/invalid → key stays unset → transport/file_sync falls
            # back to the legacy 100MB (old-proxy compatibility).
            import contextlib
            with contextlib.suppress(TypeError, ValueError):
                _state["sync_max_file_bytes"] = max(0, int(policy["sync_max_file_bytes"]))
        if "sync_ignore_rules" in policy:
            # Marker-confirmed generated-dir exclusions (0.5.110+ handshake).
            # ``None`` CLEARS the cached table — the auth path always passes
            # the key explicitly so that a proxy DOWNGRADE (rules no longer
            # sent) drops us back to legacy full-sync in lockstep with the
            # old proxy's manifest; retaining stale rules would make this
            # side exclude what the proxy includes → the diff misattributes
            # the whole tree as satellite deletes. Malformed / over-cap
            # tables also clear (predictable legacy state, mirrored by the
            # proxy's version gate). Lazy import: transport.file_sync
            # imports this module lazily in the other direction.
            from ..transport.file_sync import validate_ignore_rules
            validated = (validate_ignore_rules(policy["sync_ignore_rules"])
                         if policy["sync_ignore_rules"] is not None else None)
            if validated is not None:
                _state["sync_ignore_rules"] = validated
            else:
                if policy["sync_ignore_rules"] is not None:
                    import logging
                    logging.getLogger("satellite").warning(
                        "sync_ignore_rules rejected by validation — "
                        "falling back to legacy sync exclusions")
                _state.pop("sync_ignore_rules", None)


def get_policy() -> _Policy:
    """Return a copy of the current cached policy."""
    with _lock:
        return dict(_state)  # type: ignore[return-value]


def is_full_fs_allowed() -> bool:
    """Convenience accessor — True iff allow_full_fs is currently set."""
    return get_policy().get("allow_full_fs", False)
