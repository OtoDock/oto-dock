"""App handlers (APPS.md "Handlers"): the wakes an app's server asked for
in its manifest — a schedule, a trigger, a platform event, another app's
event — delivered durably: a row first (``storage/db_app_deliveries.py``),
the wake after, with a lease, retries and a dead end the owner can read.

Who enqueues: the runner for a schedule (a ``dynamic_tasks`` row of
``task_type='app'`` per handler, kept in step with the manifest at every
go-live), the trigger manager for a webhook, the catalog's emit sites for a
platform event, the bindings route for a cross-app event. What fires:
``POST /_handler/<name>`` on the app's own port with a signed claim of
principal ``platform`` and ``X-OtoDock-Delivery-Id``; the app forwards the
claim on its platform calls and the proxy lets the app identity write while
the delivery is in flight.

Constraints: one POST in flight per app, rows in ``next_at`` order; a
failed pre-check is ``dead`` with the reason; an unapproved app waits
(re-armed, not counted) until the day-long cap; nothing wakes while the
proxy shuts down; a drain holds its instance against the idle stop.

Steps (APPS.md "Steps") ride the same queue in their own lane: the drain
hands a step handler's row to its own task and moves on, so a running
script never holds the server's wakes; one run at a time per step handler
(the next row of that handler waits), at most ``MAX_STEPS_PER_APP`` per app
and ``MAX_STEPS_PLATFORM`` on the whole platform; a row past a cap simply
waits in the queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

import httpx

from storage import database as task_store
from storage import db_app_deliveries as deliveries
from storage.automation import run_status
from services.scheduler import task_kinds
from storage.pg import run_db

#: A delivery's verdict as the run row it carries reads it (APPS.md
#: "Handlers"): the one mapping between the two machines.
RUN_STATUS_OF = {deliveries.DONE: run_status.COMPLETED, deliveries.DEAD: run_status.FAILED}
#: ``_precheck``'s hold reason — the delivery waits for the manifest's
#: approval instead of dying; a reason word, not a delivery status.
HOLD_UNAPPROVED = "unapproved"

logger = logging.getLogger("claude-proxy.apps")

HANDLER_TIMEOUT_S = 60.0
RESULT_KEEP_BYTES = 32 * 1024
UNAPPROVED_WAIT_S = 30
# Feed → the event a handler may wake on (the emit sites of api/apps/catalog.py).
FEED_EVENTS = {
    "sessions": "chat_created",
    "tasks": "task_finished",
    "file_changes": "file_changed",
    "trigger_fires": "trigger_fired",
    "notifications": "notification_created",
    # CHECKS.md: a verdict landed; a sessions delta carrying ``turn`` is
    # ``turn_finished`` instead of ``chat_created`` (on_feed_delta).
    "check_verdicts": "check_verdict",
}

_drains: dict[str, asyncio.Task] = {}
_drain_locks: dict[str, asyncio.Lock] = {}
_drain_again: set[str] = set()
# (app id, handler) → the task running that step right now (APPS.md "Steps").
_step_runs: dict[tuple[str, str], asyncio.Task] = {}
MAX_STEPS_PER_APP = 4
MAX_STEPS_PLATFORM = 8
# (agent, event) → [(app id, handler)] for platform events, and
# (target agent, target slug, event) → [(app id, handler, binding name)]
# for cross-app events; rebuilt lazily from approved rows when marked dirty.
_index: dict[tuple[str, str], list[tuple[str, str]]] = {}
_app_event_index: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
_agents_with_events: set[str] = set()
_index_dirty = True
_index_lock: asyncio.Lock | None = None
_sweeps = 0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── the subscriber index ────────────────────────────────────────────────────


def mark_dirty() -> None:
    global _index_dirty
    _index_dirty = True


def has_subscribers(agent: str) -> bool:
    """Cheap and conservative: True while the index is stale."""
    return _index_dirty or agent in _agents_with_events


def _build_index() -> tuple[dict, dict, set]:
    from api.apps import manifest as _mf
    plat: dict[tuple[str, str], list[tuple[str, str]]] = {}
    cross: dict[tuple[str, str, str], list[tuple[str, str, str]]] = {}
    agents: set[str] = set()
    for row in task_store.list_apps_with_handlers():
        if not task_store.app_actions_approved(row):
            continue
        agent = row.get("agent") or ""
        bindings = {b.get("name"): b for b in _mf.parse_bindings(row)}
        for handler, events in (_mf.parse_handlers(row).get("on_event") or {}).items():
            for ev in events:
                if ev.startswith("app:"):
                    _, bname, name = ev.split(":", 2)
                    b = bindings.get(bname)
                    if b:
                        cross.setdefault((b.get("agent") or "", b.get("app") or "", name), []).append(
                            (row["id"], handler, bname))
                else:
                    plat.setdefault((agent, ev), []).append((row["id"], handler))
                    agents.add(agent)
    return plat, cross, agents


async def _ensure_index() -> None:
    global _index, _app_event_index, _agents_with_events, _index_dirty, _index_lock
    if not _index_dirty:
        return
    if _index_lock is None:
        _index_lock = asyncio.Lock()
    async with _index_lock:
        if not _index_dirty:
            return
        plat, cross, agents = await run_db(_build_index)
        _index, _app_event_index, _agents_with_events = plat, cross, agents
        _index_dirty = False


async def cross_app_subscribers(target_agent: str, target_slug: str, event: str) -> list[tuple[str, str, str]]:
    """``(app id, handler, binding name)`` of every approved row whose
    bindings name the emitter and whose handlers wake on its event (the
    bindings route re-checks each edge before it enqueues)."""
    await _ensure_index()
    return list(_app_event_index.get((target_agent, target_slug, event), []))


# ── enqueue ─────────────────────────────────────────────────────────────────


async def enqueue(row: dict, handler: str, event: str, payload: dict, *, event_id: str = "",
                  trigger_id: str | None = None, task_run_id: str | None = None) -> dict | None:
    """One delivery for ``row``'s ``handler``; None for a replayed
    ``event_id``. A pending row schedules its drain at once."""
    d = await run_db(deliveries.enqueue, row["id"], handler, event, payload,
                     event_id=event_id, trigger_id=trigger_id, task_run_id=task_run_id)
    if d is not None and d["status"] == deliveries.PENDING:
        schedule_drain(row["id"])
    return d


def on_feed_delta(users, agent: str, feed: str, delta: dict, shared: bool | None) -> None:
    """Called by the catalog on the loop for every delta: nothing when no
    approved app of the agent wakes on it, else a dispatch task."""
    event = FEED_EVENTS.get(feed)
    if not event:
        return
    if feed == "sessions" and delta.get("turn"):
        event = "turn_finished"       # CHECKS.md: a chat's turn ended
    elif feed == "sessions" and not delta.get("created"):
        return
    if feed == "tasks" and not delta.get("last_run"):
        return
    if event == "task_finished" and (delta.get("task_type") or "") == task_kinds.RUN_APP:
        return
    if feed == "notifications":
        agent = str(delta.get("agent_slug") or agent or "")
    if not agent or not has_subscribers(agent):
        return
    try:
        asyncio.get_running_loop().create_task(
            _dispatch(list(users), agent, feed, event, dict(delta), shared))
    except RuntimeError:
        logger.debug("handler dispatch skipped: no running loop")


async def _dispatch(users: list[str], agent: str, feed: str, event: str, delta: dict,
                    shared: bool | None) -> None:
    from api.apps import catalog
    await _ensure_index()
    subs = _index.get((agent, event)) or []
    if not subs:
        return
    payload = {"event": event, **{k: v for k, v in delta.items() if k != "removed"}}
    for app_id, handler in subs:
        row = await run_db(task_store.get_app, app_id)
        if row is None or row.get("hidden") or not task_store.app_actions_approved(row):
            continue
        if row.get("username"):
            if (row.get("owner_sub") or "") not in users:
                continue
        elif not catalog.in_agent_slice(feed, delta, shared):
            continue
        try:
            await enqueue(row, handler, event, payload)
        except Exception:
            logger.exception("App %s: could not enqueue %s", row.get("slug"), event)


# ── schedules as task rows ─────────────────────────────────────────────────


async def fire_scheduled(task, trigger_type: str = "scheduled") -> str:
    """The runner's branch for ``task_type='app'``: a run row and a delivery,
    never a session. A row whose app is gone deletes itself; a hidden or
    unapproved app is skipped (its wakes resume when it comes back)."""
    from api.apps import manifest as _mf
    from services.scheduler import scheduler as _api
    row = await run_db(task_store.get_app, task.app_id or "")
    if row is None:
        logger.info("App handler task %s: the app is gone — removing", task.id)
        await _api.remove_dynamic_task(task.id)
        return ""
    handler = task.app_handler or ""
    if row.get("hidden") or not task_store.app_actions_approved(row):
        return ""
    if handler not in (_mf.parse_handlers(row).get("on_schedule") or {}):
        return ""
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    await asyncio.to_thread(
        task_store.create_run, run_id, task.id, task.agent,
        task_kinds.TRIGGER_SCHEDULED if trigger_type == task_kinds.TRIGGER_SCHEDULED else task_kinds.TRIGGER_MANUAL,
        f"app:{row['id']}:{handler}", "", task_kinds.RUN_APP, task.scope, task.created_by,
    )
    d = await enqueue(row, handler, "schedule", {"cron": task.schedule, "task_id": task.id},
                      task_run_id=run_id)
    if d is None or d["status"] != deliveries.PENDING:
        await asyncio.to_thread(task_store.update_run, run_id, status=run_status.FAILED,
                                error_message=(d or {}).get("last_error") or "not queued",
                                completed_at=_now_iso())
    return run_id


def _task_id(row: dict, handler: str) -> str:
    return f"app-{row['id'][:8]}-{handler}-{uuid.uuid4().hex[:6]}"


async def sync_rows(row: dict) -> None:
    """Keep one ``dynamic_tasks`` row per ``on_schedule`` handler of an
    approved manifest: create the missing, re-cron the changed (a pause
    survives), remove the departed. Runs at go-live, approve and rollback."""
    import config
    from api.apps import manifest as _mf
    from core.session.session_state import get_user_tz
    from services.scheduler import scheduler as _api
    from services.scheduler import shared as _shared
    wanted = (_mf.parse_handlers(row).get("on_schedule") or {}) \
        if task_store.app_actions_approved(row) and not row.get("hidden") else {}
    existing = {t.get("app_handler"): t for t in await run_db(task_store.list_app_handler_tasks, row["id"])}
    personal = bool(row.get("username"))
    title = row.get("title") or row.get("slug") or "app"
    for handler, spec in wanted.items():
        cron = str((spec or {}).get("cron") or "").strip()
        cur = existing.pop(handler, None)
        if not cron:
            continue
        if cur is None:
            await _api.add_dynamic_task(_shared.TaskDefinition(
                id=_task_id(row, handler), name=f"{title}: {handler}", agent=row["agent"],
                prompt="", schedule=cron, task_type=task_kinds.APP,
                scope="user" if personal else "agent",
                created_by=(row.get("owner_sub") or "") if personal else row["agent"],
                notification_mode="none", timeout_seconds=int(HANDLER_TIMEOUT_S),
                user_tz=get_user_tz(row.get("owner_sub") or "") if personal else None,
                app_id=row["id"], app_handler=handler,
            ))
        elif (cur.get("schedule") or "") != cron:
            await asyncio.to_thread(task_store.update_dynamic_task, cur["id"], {"schedule": cron})
            if config.SCHEDULER_MODE != "standalone":
                fresh = await asyncio.to_thread(task_store.get_dynamic_task, cur["id"])
                if fresh and fresh.get("enabled"):
                    _api._register_task(_api._row_to_task(fresh))
    for cur in existing.values():
        await _api.remove_dynamic_task(cur["id"])
    mark_dirty()


async def remove_rows(app_id: str) -> None:
    """A delete site: the handler task rows go, the app's triggers are
    paused and detached, the event index forgets the row."""
    from services.scheduler import scheduler as _api
    from storage.automation import trigger_store
    for t in await run_db(task_store.list_app_handler_tasks, app_id):
        try:
            await _api.remove_dynamic_task(t["id"])
        except Exception:
            logger.exception("App %s: could not remove handler task %s", app_id, t["id"])
    try:
        await run_db(trigger_store.detach_app, app_id)
    except Exception:
        logger.exception("App %s: could not detach its triggers", app_id)
    mark_dirty()


# ── the drain ───────────────────────────────────────────────────────────────


def _drain_lock(app_id: str) -> asyncio.Lock:
    return _drain_locks.setdefault(app_id, asyncio.Lock())


def draining(app_id: str) -> bool:
    t = _drains.get(app_id)
    return t is not None and not t.done()


def schedule_drain(app_id: str) -> None:
    """Start the app's drain, or tell the running one to look again before
    it stops (a row enqueued while the drain was taking its last row would
    otherwise wait for the next sweep)."""
    from services.scheduler import shared as _shared
    if _shared._shutting_down:
        return
    if draining(app_id):
        _drain_again.add(app_id)
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _drains[app_id] = loop.create_task(drain(app_id))


def running_steps(app_id: str | None = None) -> set[str]:
    """The step handlers with a script running right now — of one app, or
    (as ``app:handler`` keys) of the whole platform."""
    live = {k for k, t in _step_runs.items() if not t.done()}
    if app_id is None:
        return {f"{a}:{h}" for a, h in live}
    return {h for a, h in live if a == app_id}


def _start_step(app_id: str, d: dict) -> None:
    """A step's row in its own task: the drain goes on to the next row; when
    the script ends the drain runs again for the rows that waited."""
    key = (app_id, d["handler"])

    async def _run() -> None:
        try:
            await _fire(d, as_step=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("App step %s: fire failed", d["id"])
            await run_db(deliveries.retry, d["id"], error=f"internal error: {e}")
        finally:
            _step_runs.pop(key, None)
            schedule_drain(app_id)

    _step_runs[key] = asyncio.get_running_loop().create_task(_run())


async def drain(app_id: str) -> int:
    """Fire the app's due rows one after another until none is due; a step
    row goes to its own task (``_start_step``) and the loop continues."""
    from api.apps import manifest as _mf
    from services.scheduler import shared as _shared
    fired = 0
    async with _drain_lock(app_id):
        while not _shared._shutting_down:
            row = await run_db(task_store.get_app, app_id)
            steps = set(_mf.parse_steps(row)) if row else set()
            busy = running_steps(app_id)
            skip = set(busy)
            if steps and (len(busy) >= MAX_STEPS_PER_APP
                          or len(running_steps()) >= MAX_STEPS_PLATFORM):
                skip |= steps
            d = await run_db(deliveries.claim_next, app_id, exclude_handlers=sorted(skip))
            if d is None:
                if app_id in _drain_again:
                    _drain_again.discard(app_id)
                    continue
                break
            fired += 1
            if d["handler"] in steps:
                _start_step(app_id, d)
                continue
            try:
                await _fire(d)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("App delivery %s: fire failed", d["id"])
                await run_db(deliveries.retry, d["id"], error=f"internal error: {e}")
    return fired


async def drain_due() -> None:
    """The sweep's call (and the boot's): reclaim passed leases, then start a
    drain for every app with a due row."""
    from services.scheduler import shared as _shared
    if _shared._shutting_down:
        return
    await run_db(deliveries.reclaim_expired)
    for app_id in await run_db(deliveries.due_app_ids):
        schedule_drain(app_id)


async def sweep() -> None:
    """From the supervisor's sweep: drains, and the retention every twenty
    sweeps (about ten minutes)."""
    global _sweeps
    await drain_due()
    _sweeps += 1
    if _sweeps % 20 == 0:
        await run_db(deliveries.purge_old)


async def boot() -> None:
    """Startup, after the releases reconcile: rows mid-POST at the crash go
    back to the queue, the index is rebuilt, due rows drain."""
    try:
        n = await run_db(deliveries.reset_inflight)
        if n:
            logger.info("App handlers: %d delivery(ies) back in the queue after restart", n)
        await run_db(deliveries.purge_old)
    except Exception:
        logger.exception("App handlers: boot bookkeeping failed (continuing)")
    mark_dirty()
    await drain_due()


async def cancel_drains() -> None:
    """Shutdown, before the servers stop: a cancelled drain leaves its row in
    flight for the boot reset."""
    tasks = [t for t in _drains.values() if not t.done()]
    tasks += [t for t in _step_runs.values() if not t.done()]
    for t in tasks:
        t.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _drains.clear()
    _step_runs.clear()


# ── one fire ────────────────────────────────────────────────────────────────


def _precheck(row: dict | None, d: dict) -> str:
    """Why the delivery must not fire: a reason (``dead``), ``HOLD_UNAPPROVED``
    (wait), or ``""``."""
    from api.apps import manifest as _mf
    if row is None:
        return "the app is gone"
    if not task_store.app_kind_of(row).may_serve:
        return "not a folder app"
    if row.get("hidden"):
        return "the app is unpinned"
    if row.get("scope_chat_id") or row.get("scope_project_id"):
        return "a Dock app has no handlers"
    if not task_store.app_actions_approved(row):
        return HOLD_UNAPPROVED
    if task_store.personal_row_dormant(row):
        return "the owner no longer has access to this agent"
    if d["handler"] not in _mf.handler_names(row):
        return "the handler is not in the manifest"
    if d.get("trigger_id"):
        from storage.automation import trigger_store
        trig = trigger_store.get_trigger(d["trigger_id"])
        if not trig or not trig.get("enabled") or (trig.get("app_id") or "") != row["id"]:
            return "the trigger is gone or paused"
        creator = trig.get("created_by") or ""
        if creator and creator != trig.get("agent") and not _mf.sub_can_approve_surface(creator, row):
            return "the trigger's creator may no longer wake this app"
    return ""


async def _run_update(d: dict, **fields) -> None:
    if d.get("task_run_id"):
        await asyncio.to_thread(task_store.update_run, d["task_run_id"], **fields)


async def _dead(d: dict, reason: str) -> None:
    await run_db(deliveries.finish, d["id"], deliveries.DEAD, error=reason)
    await _run_update(d, status=RUN_STATUS_OF[deliveries.DEAD], error_message=reason[:2000],
                      completed_at=_now_iso())
    if d.get("trigger_id"):
        from storage.automation import trigger_store
        await run_db(trigger_store.set_last_error, d["trigger_id"], f"{d['handler']}: {reason}")
    logger.warning("App delivery %s (%s) dead: %s", d["id"][:8], d["handler"], reason)


async def _rearm(d: dict, reason: str, delay_s: float) -> None:
    status = await run_db(deliveries.retry, d["id"], error=reason, count=False, delay_s=delay_s)
    if status == deliveries.DEAD:
        await _run_update(d, status=RUN_STATUS_OF[deliveries.DEAD], error_message=reason[:2000],
                          completed_at=_now_iso())


async def _retry(d: dict, reason: str) -> None:
    status = await run_db(deliveries.retry, d["id"], error=reason, count=True)
    if status == deliveries.DEAD:
        await _run_update(d, status=RUN_STATUS_OF[deliveries.DEAD], error_message=reason[:2000],
                          completed_at=_now_iso())
        if d.get("trigger_id"):
            from storage.automation import trigger_store
            await run_db(trigger_store.set_last_error, d["trigger_id"], f"{d['handler']}: {reason}")
    logger.info("App delivery %s (%s) %s: %s", d["id"][:8], d["handler"], status, reason)


async def _read_capped(upstream: httpx.Response) -> str:
    chunks: list[bytes] = []
    total = 0
    try:
        async for chunk in upstream.aiter_bytes():
            total += len(chunk)
            chunks.append(chunk)
            if total >= RESULT_KEEP_BYTES:
                break
    except httpx.HTTPError:
        pass
    return b"".join(chunks)[:RESULT_KEEP_BYTES].decode("utf-8", "replace").replace("\x00", "")


def basis_for(d: dict) -> str:
    """What the app sees the wake as: ``inbound`` for a vendor's event
    (APPS.md "Inbound hooks" — server-only, the claim is no write
    authority), ``platform`` for every other wake."""
    return "inbound" if str(d.get("event") or "").startswith("inbound:") else "platform"


def claim_for(d: dict, app_id: str) -> str:
    """The caller claim the app verifies: principal ``platform``, the handler
    and delivery it belongs to, the handler timeout as its life; an inbound
    wake's claim carries ``kind: inbound``, which the platform gates refuse
    as a write or press authority."""
    from services.apps import app_tokens
    claims = {
        "principal": "platform", "sub": "", "username": "", "role": "app",
        "handler": d["handler"], "delivery": d["id"], "event": d["event"],
        "trigger_id": d.get("trigger_id") or "", "external": False,
    }
    if basis_for(d) == "inbound":
        claims["kind"] = "inbound"
    return app_tokens.mint(app_id, app_tokens.PURPOSE_CALLER, claims, int(HANDLER_TIMEOUT_S))


async def _fire(d: dict, *, as_step: bool = False) -> None:
    """One delivery: the pre-checks, then the step runner or the POST.
    ``as_step`` says the drain put the row in the step lane; a manifest
    that changed underneath sends the row back to the queue for the other
    lane instead of running it in the wrong one."""
    from api.apps.app_proxy import forward
    from services.apps import app_supervisor
    row = await run_db(task_store.get_app, d["app_id"])
    why = await asyncio.to_thread(_precheck, row, d)
    if why == HOLD_UNAPPROVED:
        await _rearm(d, "the app is waiting for approval", UNAPPROVED_WAIT_S)
        return
    if why:
        await _dead(d, why)
        return
    assert row is not None
    from api.apps import manifest as _mf
    is_step = _mf.is_step(row, d["handler"])
    if is_step != as_step:
        await _rearm(d, "the handler changed lanes with the manifest", 5)
        return
    if is_step:
        # A step runs where the agent's sessions run, server or no server
        # (APPS.md "Steps"); its verdict comes back through the same store.
        from services.apps import app_steps
        await app_steps.fire(d, row)
        return
    try:
        inst = await app_supervisor.ensure_up(row)
    except app_supervisor.AppUnavailable as e:
        await _rearm(d, e.reason, max(5, int(e.retry_after)))
        return
    if inst.state == app_supervisor.STATIC:
        await _dead(d, "the app has no server")
        return
    body = json.dumps({
        "event": d["event"], "handler": d["handler"], "delivery_id": d["id"],
        "attempt": int(d.get("attempts") or 0) + 1, "payload": d.get("payload") or {},
        **({"trigger": {"id": d["trigger_id"]}} if d.get("trigger_id") else {}),
    }).encode("utf-8")
    headers = [
        ("Content-Type", "application/json"),
        ("X-OtoDock-Viewer", claim_for(d, row["id"])),
        ("X-OtoDock-Basis", basis_for(d)),
        ("X-OtoDock-Delivery-Id", d["id"]),
    ]
    await _run_update(d, status=run_status.RUNNING, started_at=_now_iso())
    app_supervisor.touch(row["id"])
    try:
        upstream = await forward(inst, "POST", f"/_handler/{d['handler']}", headers, body,
                                 timeout=HANDLER_TIMEOUT_S)
    except httpx.HTTPError as e:
        await _retry(d, f"the app's server did not answer: {e.__class__.__name__}")
        return
    text = await _read_capped(upstream)
    await upstream.aclose()
    app_supervisor.touch(row["id"])
    code = upstream.status_code
    if code < 300:
        await run_db(deliveries.finish, d["id"], deliveries.DONE)
        await _run_update(d, status=RUN_STATUS_OF[deliveries.DONE], output_text=text,
                          completed_at=_now_iso())
    elif code < 500:
        await _dead(d, f"the handler answered {code}: {text[:200]}")
    else:
        await _retry(d, f"the handler answered {code}: {text[:200]}")


# ── what the status shows ──────────────────────────────────────────────────


def wakes(row: dict, limit: int = 10) -> list[dict]:
    """The last wakes, newest first; a step's row carries its exit code and
    where it ran (``local`` or a machine id)."""
    return [{"id": w["id"], "handler": w["handler"], "event": w["event"], "status": w["status"],
             "attempts": int(w.get("attempts") or 0), "error": w.get("last_error") or "",
             "at": w.get("created_at") or "", "done_at": w.get("done_at") or "",
             "exit_code": w.get("exit_code"), "ran_on": w.get("ran_on") or ""}
            for w in deliveries.recent(row["id"], limit)]
