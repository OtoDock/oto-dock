"""What the stream pump's event handlers read off ``self``.

``ChatStreamPump`` (``stream_pump.py``) takes its per-event handlers from
``PumpEventHandlers`` (``pump_event_handlers.py``), which reads the pump's
state and calls its helpers through ``self``. A type checker sees the mixin
alone, so the mixin names this Protocol as its base for the type checker
only (``if TYPE_CHECKING``); at run time the base is ``object``, so nothing
about the class changes.

A new attribute the pump sets, or a pump helper a handler calls, is declared
here.
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol


class PumpHost(Protocol):
    _active_tools: dict[str, dict[Any, Any]]
    _llm_cost_delta: float
    _mcp_cost_by_key: dict[tuple[str, str], float]
    _pending_text: list[str]
    _tool_seen: int
    _live_twins: dict[int, tuple[dict[Any, Any], dict[Any, Any]]]
    _spawns_done: set[str]
    _todo_ref: Any
    _total_cost_delta: float
    _turn_blocks: list[dict[Any, Any]]
    _wf_last_forward: dict[str, float]
    chat_id: str
    session_id: str

    async def _clear_orphan_thinking(self, live: dict | None): ...
    def _engine_turn_ended(self) -> None: ...
    async def _cost_billed_for(self, session_id: str) -> bool: ...
    async def _flush_pending_previews(self): ...
    def _flush_pending_text(self): ...
    async def _forward(self, item: dict): ...
    async def _forward_delta(self, event_type: str, payload: dict): ...
    async def _forward_event(self, event_type: str, payload: dict): ...
    def _live_append_text(self, live: dict, text: str, note: bool | None = None): ...
    async def _queue_or_show_permission(self, perm_data: dict): ...
    def _rewrite_row(self, ref: Any, block: dict) -> None: ...
    def _save_turn_blocks(self, *, note_range: bool = False, early: bool = False,
                          upto: int | None = None) -> asyncio.Future | None: ...
    @classmethod
    def _skips_tool_persist(cls, name: str) -> bool: ...
    def _stamp_speaker(self, block: dict) -> dict: ...
    def _submit_chat_update(self, label: str, **fields) -> None: ...
