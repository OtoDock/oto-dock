"""What the satellite connection manager's mixins read off ``self``.

``SatelliteConnectionManager`` (``satellite_connection.py``) is assembled
from the grace, admin-alert and file-transfer mixins, which share the
manager's state and call its helpers through ``self``. A type checker sees
each mixin alone, so each names this Protocol as its base for the type
checker only (``if TYPE_CHECKING``); at run time the base is ``object``, so
nothing about the class changes.

A new attribute the manager sets, or a manager helper a mixin calls, is
declared here.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from core.remote.satellite_connection import SatelliteConnection
    from core.remote.satellite_file_transfer import _FileChangedLane, _PullStream


class SatelliteManagerHost(Protocol):
    _connections: dict[str, SatelliteConnection]
    _fc_global_sem: asyncio.Semaphore
    _fc_lanes: dict[str, _FileChangedLane]
    _grace_sessions: dict[str, dict[str, tuple[asyncio.Queue[Any], str]]]
    _grace_timers: dict[str, asyncio.Task[Any]]
    _lock: asyncio.Lock
    _pending_acks: dict[str, tuple[str, asyncio.Future[Any], SatelliteConnection | None]]
    _pending_pulls: dict[str, _PullStream]
    _pty_grace_timers: dict[str, asyncio.Task[Any]]

    def effective_sync_cap(self, machine_id: str) -> int: ...
    def satellite_supports_paced_transfers(self, machine_id: str) -> bool: ...
    async def send_command(
        self,
        machine_id: str,
        msg: dict,
        *,
        timeout: float = 30.0,
        command_id: str | None = None,
    ) -> dict: ...
    async def send_fire_and_forget(self, machine_id: str, msg: dict) -> None: ...
