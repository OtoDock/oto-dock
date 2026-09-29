"""The task-run SSE stream's frames (``GET /v1/tasks/runs/{run_id}/stream``):
the builders, the two questions the loop asks of a frame, the keep-alive
cadence and the row backstop — the endpoint's parts that are not the
endpoint (``api/tasks/tasks.py``). The frame words (``status`` / ``text`` /
``tool_start`` / ``tool_end`` / ``task_spawn`` / ``done`` / ``error``) are
the stream's own, read by schedules-mcp's ``run_task(wait=true)``; the
``done`` frame's status is the run vocabulary's, sent by the runner after
the row is stamped (``services/scheduler/runner.py``).
"""

import asyncio
import json

from storage import database as task_store
from storage.automation import run_status

#: The keep-alive comment's cadence, and how long a subscriber can outlive a
#: missed broadcast: the loop re-reads the row on every tick.
KEEPALIVE_S = 30.0


def sse(frame: dict) -> str:
    return f"data: {json.dumps(frame)}\n\n"


def text_frame(text: str) -> dict:
    return {"type": "text", "text": text}


def done_frame(status: str) -> dict:
    return {"type": "done", "status": status}


def is_text(frame: dict) -> bool:
    return frame.get("type") == "text"


def is_done(frame: dict) -> bool:
    return frame.get("type") == "done"


async def row_end(run_id: str) -> dict | None:
    """The run row when it has ended, else ``None``."""
    run = await asyncio.to_thread(task_store.get_run, run_id)
    if run and run_status.is_terminal(run.get("status")):
        return run
    return None
