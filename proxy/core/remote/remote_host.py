"""What the remote layer's mixins read off ``self``.

``RemoteExecutionLayer`` (``remote_execution.py``) is assembled from the
session-start, start-payload, turn, abort, resume, background-command and
workspace-sync mixins, which share the layer's state and call each other's
helpers through ``self``. A type checker sees each mixin alone, so each names
this Protocol as its base for the type checker only (``if TYPE_CHECKING``);
at run time the base is ``object``, so nothing about the class changes.

A new attribute the layer sets, or a helper one mixin calls on another, is
declared here. Never a member the layer gets only from ``ExecutionLayer``
(listed after the mixins): the Protocol's stub would shadow it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from core.execution_layer import AgentConfig, LayerCapabilities, RemoteStartPlan
    from core.remote.remote_session_info import RemoteSessionInfo
    from core.remote.satellite_connection import SatelliteConnectionManager


class RemoteLayerHost(Protocol):
    _cm: SatelliteConnectionManager
    _deferred_sync_tasks: set[asyncio.Task[Any]]
    _sessions: dict[str, RemoteSessionInfo]
    _spawning: set[str]

    @staticmethod
    def _adapter(info: RemoteSessionInfo): ...
    async def _build_start_payload(
        self, session_id: str, config: AgentConfig, execution_path: str,
    ) -> RemoteStartPlan: ...
    async def _hard_abort(self, session_id: str) -> bool: ...
    async def _initial_workspace_sync(
        self, machine_id: str, agent_slug: str,
        *, target_username: str | None = None,
        target_role: str = "",
        session_username: str = "",
        session_knowledge_rw: bool = False,
        progress_cb=None,
        background: bool = False,
    ) -> None: ...
    async def _insert_session(self, info: RemoteSessionInfo) -> None: ...
    def capabilities_for(self, session_id: str) -> LayerCapabilities: ...
    async def close_session(self, session_id: str) -> None: ...
    async def is_session_alive(self, session_id: str) -> bool: ...
