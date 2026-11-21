"""``GET /metrics``: the few numbers an operator watches, in Prometheus's text
format (0.0.4).

Never a secret and never a person's data: no label names a person, an agent,
a chat or a tool, and every number is a count or a state the admin health
already shows. Read from memory only, like the health answer.

Who may read it: a platform admin signed in, the bearer
``OTODOCK_METRICS_TOKEN`` (a scraper's, off while empty), or the master key
from the server's own machine (the admin command's rule, for a controller
inside the install). Nobody else: no credential is 401, any other one 403.
A session's token, a person's API key and the master key from elsewhere are
refused; the route is in no public description.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse

import config
from api.internal.internal import _require_admin_command
from auth.providers import UserContext, get_current_user, require_admin

router = APIRouter()

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header[:7].lower() == "bearer " else ""


def _metrics_token(request: Request) -> bool:
    token = config.METRICS_TOKEN
    presented = _bearer(request)
    return bool(token) and bool(presented) and hmac.compare_digest(
        presented.encode("utf-8", "surrogatepass"), token.encode("utf-8", "surrogatepass"))


def _authorize(request: Request, user: UserContext | None) -> None:
    if _metrics_token(request):
        return
    if user is not None and user.is_service:
        _require_admin_command(request, user)
        return
    require_admin(user)


def _line(out: list[str], name: str, value: float | int | None, help_text: str,
          kind: str = "gauge", labels: dict[str, str] | None = None) -> None:
    if value is None:
        return
    if not any(line.startswith(f"# HELP {name} ") for line in out):
        out.append(f"# HELP {name} {help_text}")
        out.append(f"# TYPE {name} {kind}")
    label = ""
    if labels:
        label = "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"
    out.append(f"{name}{label} {value}")


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render() -> str:
    """The exposition, from the health parts, the busy-or-idle answer and the
    process's own counters."""
    from core import drain, log_queue, loop_watchdog
    from core.remote.satellite_connection import get_connection_manager
    from services.infra import health_parts
    from storage import pg

    out: list[str] = []
    parts = health_parts.parts()
    work = drain.work()
    _line(out, "otodock_up", 1, "The server answers.")
    _line(out, "otodock_ready", int(health_parts.ready(parts)),
          "The server takes new work: the database answers and nothing holds it.")
    _line(out, "otodock_build_info", 1, "The version this server runs.",
          labels={"version": config.PINNED_OTODOCK_VERSION or ""})
    for part, word in parts.items():
        if word != health_parts.OFF:
            _line(out, "otodock_part_ok", int(word == health_parts.OK),
                  "A part of the server works (database, tools, scheduler, phone).",
                  labels={"part": part})
    _line(out, "otodock_holding", int(bool(work.get("holding"))), "New work is held.")
    _line(out, "otodock_starting", int(bool(work.get("starting"))),
          "New work is held while the tools are prepared after an update.")
    for kind in drain.KINDS:
        _line(out, "otodock_open_work", work["open"].get(kind, 0),
              "What is working right now, per kind.", labels={"kind": kind})
    _line(out, "otodock_waiting_on_people", work["waiting_on_people"],
          "Turns that wait for a person's answer.")
    _line(out, "otodock_queued_messages", work["queued"], "Messages that wait to be sent.")
    _line(out, "otodock_sessions", work["sessions"], "Sessions live on this server.")
    _line(out, "otodock_idle_seconds", work["idle_s"],
          "Seconds since anything worked or waited to be sent.")
    try:
        machines = len(get_connection_manager().get_connected_machines())
    except Exception:  # noqa: BLE001  # one source must not fail the scrape
        machines = None
    _line(out, "otodock_machines_connected", machines, "Remote machines connected now.")
    loop = loop_watchdog.stats()
    _line(out, "otodock_loop_stalls_total", loop.get("stalls"),
          "Event loop stalls since the start.", kind="counter")
    _line(out, "otodock_loop_stall_seconds_total", loop.get("stall_seconds_total"),
          "Seconds the event loop stalled since the start.", kind="counter")
    _line(out, "otodock_log_dropped_total", log_queue.stats().get("dropped"),
          "Log records dropped since the start.", kind="counter")
    fds = loop_watchdog.fd_stats()
    _line(out, "otodock_open_fds", fds.get("open"), "Open file descriptors.")
    _line(out, "otodock_max_fds", fds.get("limit"), "The file descriptor limit.")
    shared = pg.pool_stats().get("shared") or {}
    _line(out, "otodock_db_pool_size", shared.get("pool_size"), "Database connections open.")
    _line(out, "otodock_db_pool_available", shared.get("pool_available"),
          "Database connections free.")
    _line(out, "otodock_db_requests_waiting", shared.get("requests_waiting"),
          "Requests that wait for a database connection.")
    return "\n".join(out) + "\n"


@router.get("/metrics")
async def metrics(request: Request, user: UserContext | None = Depends(get_current_user)):
    _authorize(request, user)
    return PlainTextResponse(render(), media_type=CONTENT_TYPE)
