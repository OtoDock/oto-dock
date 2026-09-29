"""Declared-action execution (``/v1/apps/{id}/approve``, ``/actions``,
``/actions/batch``) — APPS.md "Action execution".

Buttons in app JS call ``otodock.action(id, args)``: declared ids only,
validated against the user-approved manifest (api/apps/manifest.py).
fire_task and mcp_tool execute HERE; send_prompt rides the chat WS
(ws/dashboard_chat.py) with the backchannel authority downgrades. Page args
NEVER reach a prompt or a tool un-gated: fire_task substitutes them only
through a user-approved ``args_schema`` (schema-less = verbatim, args
rejected), and mcp_tool validates them against its schema then merges UNDER
the declared ``fixed_args`` before the headless executor
(services/apps/headless_exec.py) runs the one declared tool. Split out of
``apps.py`` (the size cap) — the rate dict stays there so every caller and
test shares one.
"""

import asyncio
import hashlib
import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.apps import manifest as _mf
from api.apps.apps import (
    _can_approve_surface,
    _check_fire_rate,
    _manifest_target_error,
    _visible_row,
)
from auth.providers import UserContext, get_current_user, require_auth, require_human
from storage import database as task_store

logger = logging.getLogger("claude-proxy.apps")
router = APIRouter()


class ApproveRequest(BaseModel):
    sig: str


@router.post("/v1/apps/{app_id}/approve")
async def approve_app(
    app_id: str,
    req: ApproveRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Approve the declared-actions manifest. The body carries the sig the
    client RENDERED, so a manifest mutated after the approval card was shown
    is refused (409) — the user only ever approves actions they saw. The
    approver must hold run authority for every fire_task target: approval
    is what delegates the run to every app viewer.

    Human-only (``require_human``): the session JWT an agent sandbox holds
    resolves to its owner's role, so on the role alone an editor-owned
    session could pin a manifest and approve it itself, and every button
    would then run on the editor's standing authority with nobody having
    seen the card."""
    u = require_human(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_approve_surface(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to approve this app's actions")

    err = await asyncio.to_thread(_manifest_target_error, row, u)
    if err:
        raise HTTPException(status_code=403, detail=err)
    ok = await asyncio.to_thread(task_store.approve_app_actions, app_id, req.sig, u.sub)
    if not ok:
        raise HTTPException(status_code=409, detail="The manifest changed — review it again")
    # An approval here (the plain card — the deploy card runs its own
    # go-live) is also what makes the app's handlers real: its schedules
    # become task rows and its events reach the index (APPS.md "Handlers").
    if task_store.app_kind_of(row).may_serve:
        from services.apps import app_handlers
        fresh = await asyncio.to_thread(task_store.get_app, app_id)
        try:
            await app_handlers.sync_rows(fresh or row)
        except Exception:
            logger.exception("App %s: handler rows out of step after approval", row.get("slug"))
    return {"status": "ok"}


class ActionRequest(BaseModel):
    args: Any = None


def _validate_action_args(action: dict, args) -> dict:
    """Gate page-supplied args behind the action's user-approved schema.
    Schema-less actions take NO args (fail-closed: unexpected input is
    refused, not dropped). Raises HTTPException on any mismatch."""
    schema = action.get("args_schema")
    if not schema:
        if args:
            raise HTTPException(status_code=400, detail="This action takes no arguments")
        return {}
    validated, err = _mf.validate_args(schema, args)
    if err:
        raise HTTPException(status_code=400, detail=err)
    return validated


def merge_checks(task_checks: list[str], action_checks: list[str]) -> list[str]:
    """CHECKS.md: a button's checks (names of the agent's checks) join the
    task's refs for the pressed run; a name that is no check on the agent
    stays a visible error verdict at turn end, never a silent skip."""
    return list(dict.fromkeys([*(task_checks or []),
                               *(f"agent:{n}" for n in action_checks if isinstance(n, str) and n)]))


async def _run_action(row: dict, action_id: str, args, u: UserContext | None,
                      *, actor: str = "", handler: str = "") -> dict:
    """Execute one declared fire_task or mcp_tool action on an accessible
    row; every refusal is an HTTPException (the single route surfaces it as
    the response, the batch route as that entry's line). send_prompt
    actions are delivered through the chat WS instead.

    Page args only ever pass through the action's user-approved
    ``args_schema``: a schema-less fire_task fires VERBATIM (args rejected —
    a page-controlled prompt_override would be prompt injection with full
    task authority); with a schema, validated args substitute into the task
    prompt. mcp_tool merges validated args UNDER the declared fixed_args and
    runs the one declared tool headlessly (no agent session, no LLM turn).

    ``actor`` names a caller that is not a platform user (``share:<id>``,
    an external link): it keys the rate limits and the log, the floor sees
    a viewer, and a task run records the share as its trigger source. No
    principal object is ever built for a link (SHARING.md).

    ``handler`` names the app's own handler pressing the button from inside
    a delivery (APPS.md "Handlers"): unattended, so the floor is the
    approval itself (the card said wakes may press the buttons), and a task
    run records ``app:<row id>:<handler>`` as its source."""
    app_id = row["id"]
    who = actor or (u.sub if u else "")
    action = _mf.find_action(row, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Unknown action")
    if action.get("type") == "send_prompt":
        raise HTTPException(status_code=400, detail="This action is delivered through the chat")
    if action.get("type") == "data_feed":
        raise HTTPException(status_code=400,
                            detail="Feeds are answered by the host page, not fired")
    if action.get("type") == "platform":
        raise HTTPException(status_code=400,
                            detail="Platform methods are called through otodock.platform, not fired")
    if not task_store.app_actions_approved(row):
        raise HTTPException(status_code=409, detail="Actions not approved")
    if not handler and not _mf.meets_floor(action, _mf.caller_role(row, u)):
        raise HTTPException(status_code=403, detail=_mf.floor_reason(action))

    if action.get("type") == "mcp_tool":
        # Click-time re-checks: the APPROVER's standing surface authority
        # (mcp_tool runs with real credentials on their delegation — a
        # demoted approver fails closed) and the MCP's availability.
        if not await asyncio.to_thread(
            _mf.sub_can_approve_surface, row.get("approved_by") or "", row,
        ):
            raise HTTPException(status_code=409, detail="Approval stale — re-approve this app's actions")
        keys = await asyncio.to_thread(_mf.assigned_mcp_keys, row["agent"])
        if keys.get(action.get("mcp") or "") != action.get("mcp"):
            raise HTTPException(status_code=409, detail="This action's MCP is no longer available")
        validated = _validate_action_args(action, args)
        merged = _mf.merge_fixed_args(action.get("fixed_args") or {}, validated)
        from services.apps import headless_exec
        # `${account.email}` in an argument (fixed or page-supplied) is the
        # connected account the call runs with — an app never asks a
        # viewer to type the address a Google tool wants (APPS.md "Account
        # arguments").
        merged = await asyncio.to_thread(headless_exec.fill_account_args, row, action, merged)
        merged_json = json.dumps(merged, sort_keys=True, separators=(",", ":"))
        if len(merged_json.encode("utf-8")) > 8192:
            raise HTTPException(status_code=400, detail="Arguments too large")
        # Args-aware, shorter interval for direct tool calls: the schema
        # bounds every value and the headless in-flight guard + the tool's
        # own latency do the heavy limiting. Identical repeat calls (a
        # toggle double-press) still wait the full second.
        args_key = hashlib.sha256(merged_json.encode("utf-8")).hexdigest()[:16]
        _check_fire_rate(app_id, action_id, who, args_key=args_key,
                         interval=2.0 if actor else 1.0)
        return await headless_exec.execute_app_tool(row, action, merged)

    # fire_task
    task_id = action.get("task_id") or ""
    dyn = await asyncio.to_thread(task_store.get_dynamic_task, task_id)
    # Re-checks at click time: the task and the APPROVER's authority may
    # both have changed since approval (edited/rescoped task, demoted
    # approver). Stale approval fails closed until someone re-approves.
    err = await asyncio.to_thread(
        _mf.check_task_target, task_id, row["agent"], not row.get("username"),
    )
    if err:
        raise HTTPException(status_code=409, detail=err)
    if not await asyncio.to_thread(
        _mf.sub_can_run_task, row.get("approved_by") or "", dyn or {},
    ):
        raise HTTPException(status_code=409, detail="Approval stale — re-approve this app's actions")
    validated = _validate_action_args(action, args)
    _check_fire_rate(app_id, action_id, who)

    from services.scheduler import scheduler
    task_def = scheduler._row_to_task(dyn)
    task_def.checks = merge_checks(task_def.checks, action.get("checks") or [])
    prompt_override = None
    if action.get("args_schema"):
        # Safe now: the values are schema-bounded (type/enum/length) and the
        # SCHEMA was what the user approved — never free-form page text.
        from services.scheduler.trigger_manager import _substitute_placeholders
        prompt_override = _substitute_placeholders(task_def.prompt or "", validated) or ""
        if len(prompt_override) > 8000:
            raise HTTPException(status_code=400, detail="Prompt too large after substitution")
    from services.scheduler import task_kinds
    if handler:
        source, trigger_type = f"app:{row['id']}:{handler}", task_kinds.TRIGGER_APP_HANDLER
    elif actor.startswith("share:"):
        source, trigger_type = f"{actor}:{action_id}", task_kinds.TRIGGER_APP_ACTION
    else:
        source, trigger_type = f"{row['slug']}:{action_id}", task_kinds.TRIGGER_APP_ACTION
    run_id = await scheduler.trigger_task_now(
        task_def, trigger_type=trigger_type,
        trigger_source=source,
        prompt_override=prompt_override,
    )
    logger.info(
        f"App action fired: app={row['slug']}, action={action_id}, "
        f"task={task_id}, by={who[:24]}, run={run_id}"
    )
    return {"status": "ok", "run_id": run_id}


class BatchCall(BaseModel):
    call_id: str
    action_id: str
    args: Any = None


class BatchRequest(BaseModel):
    calls: list[BatchCall]


BATCH_MAX_CALLS = 16
BATCH_MIN_INTERVAL_S = 1.0


async def _delivery_caller(request: Request, app_id: str) -> tuple[dict, dict] | None:
    """The app's own server pressing a button from inside a handler (APPS.md
    "Handlers"): the launch token as the bearer plus the forwarded claim of
    principal ``platform`` whose delivery is in flight. None when the bearer
    is not a launch token (the cookie path decides then)."""
    from api.apps import app_proxy
    from services.apps import app_tokens
    token = app_proxy._bearer(request.headers)
    if not token or not app_tokens.verify(token, app_id, app_tokens.PURPOSE_LAUNCH):
        return None
    row = await asyncio.to_thread(task_store.get_app, app_id)
    if not row:
        return None
    if await asyncio.to_thread(task_store.personal_row_dormant, row):
        raise HTTPException(status_code=404, detail="App not found")
    await app_proxy.caller_from_token(token, row)   # 401 unless the running instance's
    forwarded = request.headers.get("x-otodock-viewer", "")
    d = await app_proxy.inflight_delivery(forwarded, row)
    # An inbound wake never presses a button (APPS.md "Inbound hooks"): a
    # vendor's event starts no agent turn and no tool call.
    if (app_tokens.verify(forwarded, app_id, app_tokens.PURPOSE_CALLER) or {}).get("kind") == "inbound":
        raise HTTPException(status_code=403, detail="not available to an inbound wake")
    return row, d


async def _step_caller(request: Request, app_id: str) -> tuple[dict, dict] | None:
    """A step's script pressing a button from inside its run (APPS.md
    "Steps"): the bearer is the step's own caller claim, in flight. None
    when the bearer is not a step claim."""
    from api.apps import app_proxy
    row = await asyncio.to_thread(task_store.get_app, app_id)
    if not row:
        return None
    got = await app_proxy.step_caller(request.headers, row)
    if got is None:
        return None
    # Dormancy is judged only for a verified step claim: an anonymous call
    # learns nothing about the app and gets the cookie path's 401.
    if await asyncio.to_thread(task_store.personal_row_dormant, row):
        raise HTTPException(status_code=404, detail="App not found")
    return row, got[1]


@router.post("/v1/apps/{app_id}/actions/batch")
async def run_app_actions_batch(
    app_id: str,
    req: BatchRequest,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Several declared actions in one request: one auth and access pass,
    then every entry runs exactly as the single route would, concurrently
    (a manager fans out up to its call cap), with at most one fire_task per
    batch. The response streams one JSON line per entry as it completes,
    ``{call_id, action_id, ...}`` around the single route's body — a refusal
    is that entry's ``{"status": "denied", "reason", "code"}``, never an
    HTTP error for the batch — so a panel fills as its calls land. The
    batch itself is limited to one per second per (app, viewer); the host
    page paces its calls to that."""
    handler, actor = "", ""
    if user is None:
        got = await _delivery_caller(request, app_id)
        if got is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        row, d = got
        handler, actor = d["handler"], f"delivery:{d['id']}"
        u = None
    else:
        u = require_auth(user)
        row = await asyncio.to_thread(_visible_row, app_id, u)
        if not row:
            raise HTTPException(status_code=404, detail="App not found")
    calls = req.calls
    if not 1 <= len(calls) <= BATCH_MAX_CALLS:
        raise HTTPException(status_code=400,
                            detail=f"a batch carries 1 to {BATCH_MAX_CALLS} calls")
    ids = [c.call_id for c in calls]
    if len(set(ids)) != len(ids) or not all(0 < len(i) <= 64 and i.isprintable() for i in ids):
        raise HTTPException(status_code=400, detail="call_id must be unique, 1 to 64 characters")
    _check_fire_rate(app_id, "\x00batch", actor or u.sub, interval=BATCH_MIN_INTERVAL_S)

    fire_seen = False
    entries: list[tuple[BatchCall, bool]] = []
    for call in calls:
        action = _mf.find_action(row, call.action_id) or {}
        fires = action.get("type") == "fire_task"
        entries.append((call, fires and fire_seen))
        fire_seen = fire_seen or fires

    async def _one(call: BatchCall, extra_fire: bool) -> dict:
        out: dict = {"call_id": call.call_id, "action_id": call.action_id}
        try:
            if extra_fire:
                raise HTTPException(status_code=429, detail="One task run per batch")
            out.update(await _run_action(row, call.action_id, call.args, u,
                                         actor=actor, handler=handler))
        except HTTPException as e:
            out.update({"status": "denied", "reason": e.detail, "code": e.status_code})
        except Exception:
            # One entry's failure is its own line: a raise here would end
            # the stream and lose every sibling's result.
            logger.exception("App %s: batch entry %s failed", row.get("slug"), call.action_id)
            out.update({"status": "denied", "reason": "the action failed", "code": 500})
        return out

    tasks = [asyncio.ensure_future(_one(c, extra)) for c, extra in entries]

    async def _lines():
        for fut in asyncio.as_completed(tasks):
            yield json.dumps(await fut, separators=(",", ":")) + "\n"

    return StreamingResponse(
        _lines(), media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v1/apps/{app_id}/actions/{action_id}")
async def run_app_action(
    app_id: str,
    action_id: str,
    request: Request,
    req: ActionRequest | None = None,
    user: UserContext | None = Depends(get_current_user),
):
    """One declared action (``_run_action``); the batch route is the same
    execution for several. Without a user, the app's own handler may be
    pressing (``_delivery_caller``)."""
    if user is None:
        got = await _delivery_caller(request, app_id)
        if got is None:
            got = await _step_caller(request, app_id)
        if got is None:
            raise HTTPException(status_code=401, detail="Not authenticated")
        row, d = got
        return await _run_action(row, action_id, req.args if req else None, None,
                                 actor=f"delivery:{d['id']}", handler=d["handler"])
    u = require_auth(user)
    row = await asyncio.to_thread(_visible_row, app_id, u)
    if not row:
        raise HTTPException(status_code=404, detail="App not found")
    return await _run_action(row, action_id, req.args if req else None, u)
