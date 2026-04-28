"""Pinned mini-apps — serve + CRUD + fire_task execution (``/v1/apps``).

Standing agent-authored dashboards: the registry row names a workspace
``.html`` file, served COOKIE-AUTHED (no capability tokens — a standing
surface has stable identity) into the same opaque-origin sandbox as
``/v1/ui`` (every helper reused from ``api.media.ui``, CSP on every
branch). Access rule mirrors ``can_serve_token`` discipline: personal rows
serve only their owner, shared rows anyone assigned to the agent; denied
is the SAME 404 as missing (no oracle).

Actions: buttons in app JS call ``otodock.action(id, args)`` — declared ids
only, validated against the user-approved manifest (api/apps/manifest.py).
fire_task and mcp_tool execute HERE; send_prompt rides the chat WS
(ws/dashboard_chat.py) with the backchannel authority downgrades. Page args
NEVER reach a prompt or a tool un-gated: fire_task substitutes them only
through a user-approved ``args_schema`` (schema-less = verbatim, args
rejected), and mcp_tool validates them against its schema then merges UNDER
the declared ``fixed_args`` before the headless executor
(services/apps/headless_exec.py) runs the one declared tool.
"""

import asyncio
import hashlib
import html as html_escape
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import config
from api.apps import manifest as _mf
from api.media.ui import (
    _placeholder,
    _ui_response,
    is_full_document,
    request_origin,
    wrap_fragment,
)
from auth.providers import (
    UserContext,
    get_current_user,
    require_agent_access,
    require_auth,
)
from storage import database as task_store

logger = logging.getLogger("claude-proxy.apps")
router = APIRouter()

# Static action-runtime extension (NEVER per-row interpolation — see
# wrap_fragment). window.otodock exists: UI_RUNTIME defines it just above.
# The action_result listener mirrors UI_RUNTIME's action_ack bridge: the host
# posts an mcp_tool's result back into the frame, re-fired in-page as an
# `otodock:action-result` window event so buttons can render tool output.
# otodock.feed(name, cb) subscribes to a declared read-only platform feed —
# the HOST answers from the viewer's authenticated context (`feed_update`
# messages: initial snapshot on subscribe, pushes on change); the frame
# itself still has no network. cb(rows, error) — error non-null when the
# feed is undeclared/unapproved/unavailable in this view.
APP_RUNTIME = """<script>
window.otodock.action = function(id, args){
  parent.postMessage({source:'otodock-artifact', v:1, type:'app_action',
    id:String(id||''), args:(args===undefined?null:args)}, '*');
};
window.otodock.feed = function(name, cb){
  var n = String(name||'');
  var reg = window.__otodockFeeds = window.__otodockFeeds || {};
  (reg[n] = reg[n] || []).push(typeof cb === 'function' ? cb : function(){});
  parent.postMessage({source:'otodock-artifact', v:1, type:'feed_subscribe',
    feed:n}, '*');
};
addEventListener('message', function(e){
  if (!e.data || e.data.source !== 'otodock-host') return;
  if (e.data.type === 'action_result'){
    try {
      window.dispatchEvent(new CustomEvent('otodock:action-result', {
        detail: {id: String(e.data.id || ''), ok: !!e.data.ok,
                 result: String(e.data.result || '')}
      }));
    } catch (err) {}
  }
  if (e.data.type === 'feed_update'){
    var subs = (window.__otodockFeeds || {})[String(e.data.feed || '')] || [];
    for (var i = 0; i < subs.length; i++){
      try { subs[i](e.data.rows || [], e.data.error || null); } catch (err) {}
    }
  }
});
// Content-height reporting: hosts that size the frame to its content (the
// Dock's single-scroll layout) listen for this; fixed-height hosts ignore it.
// ResizeObserver catches feed-driven re-renders; 'load' covers the initial
// paint on browsers that fire RO before layout settles.
(function(){
  var last = 0;
  function report(){
    var d = document.documentElement;
    var h = Math.ceil(Math.max(d ? d.scrollHeight : 0,
                               document.body ? document.body.scrollHeight : 0));
    if (h > 0 && Math.abs(h - last) > 2){
      last = h;
      parent.postMessage({source:'otodock-artifact', v:1, type:'content_height',
        height:h}, '*');
    }
  }
  if (typeof ResizeObserver === 'function'){
    new ResizeObserver(report).observe(document.documentElement);
  } else {
    setInterval(report, 2000);
  }
  addEventListener('load', report);
})();
</script>"""

# Minimum seconds between fires of the same button by the same user. Module
# level so it survives reconnects; the task dedup guard bounds concurrency.
_FIRE_MIN_INTERVAL_S = 2.0
_fire_rate: dict[tuple[str, str, str], float] = {}


def app_access(row: dict, user: UserContext) -> bool:
    """May ``user`` see/serve this app? Personal rows → owner only; shared
    rows → anyone assigned to the agent; admin always."""
    if user.is_admin:
        return True
    if row.get("username"):
        return (row.get("owner_sub") or "") == user.sub
    return user.can_access_agent(row.get("agent") or "")


def _viewer_username(user: UserContext) -> str:
    u = task_store.get_user(user.sub)
    return (u.get("username") or "") if u else ""


def _can_approve_surface(row: dict, user: UserContext) -> bool:
    """The APP surface of approval authority (the task surface is checked
    per fire_task action)."""
    if user.is_admin:
        return True
    if row.get("username"):
        return (row.get("owner_sub") or "") == user.sub
    return user.can_edit_agent(row.get("agent") or "")


def _can_manage(row: dict, user: UserContext) -> bool:
    """Unpin / reorder authority for this row."""
    return _can_approve_surface(row, user)


@router.get("/v1/apps/{app_id}/html")
async def serve_app(
    app_id: str,
    request: Request,
    user: UserContext | None = Depends(get_current_user),
):
    """Serve a pinned mini-app (sandboxed on every branch — see
    ``api.media.ui._ui_response``)."""
    origin = request_origin(request)
    if user is None:
        return _ui_response(
            _placeholder("Sign in to OtoDock to view this mini-app."), origin, 401,
        )
    row = await asyncio.to_thread(task_store.get_app, app_id)
    # Access-denied is the SAME 404 as missing (no liveness oracle); a
    # soft-unpinned row is gone to every viewer surface.
    if not row or row.get("hidden") or not app_access(row, user):
        return _ui_response(_placeholder("This mini-app no longer exists."), origin, 404)
    path = config.get_agent_dir(row["agent"]) / row["rel_path"]
    if not path.is_file():
        name = html_escape.escape(path.name)
        return _ui_response(
            _placeholder(f"The mini-app file <code>{name}</code> was deleted from the workspace."),
            origin, 404,
        )
    content = await asyncio.to_thread(path.read_text, "utf-8", "replace")
    if is_full_document(content):
        return _ui_response(content, origin)
    return _ui_response(wrap_fragment(content, runtime_extra=APP_RUNTIME), origin)


def shape_app_rows(rows: list[dict], u: UserContext) -> list[dict]:
    """Registry rows → the client app shape (approval/staleness derived
    live). Shared by the standing list below and the chat Dock pins route
    (``api/agents/chats.py``) so scoped pins carry the exact same approval
    semantics. Synchronous — call via ``asyncio.to_thread``."""
    # Keyed per agent: the pins route may mix rows from different agents
    # (a project spans agents), and mcp availability is per-agent.
    mcps_by_agent: dict[str, dict[str, str]] = {}
    out = []
    for row in rows:
        agent = row.get("agent") or ""
        actions = _mf.parse_actions(row)
        approved = task_store.app_actions_approved(row)
        stale = False
        has_mcp_tool = any(a.get("type") == "mcp_tool" for a in actions)
        if has_mcp_tool and agent not in mcps_by_agent:
            mcps_by_agent[agent] = _mf.assigned_mcp_keys(agent)
        for a in actions:
            if a.get("type") == "mcp_tool":
                a["mcp_available"] = mcps_by_agent.get(agent, {}).get(
                    a.get("mcp") or "") == a.get("mcp")
        if approved and actions:
            for a in actions:
                if a.get("type") != "fire_task":
                    continue
                dyn = task_store.get_dynamic_task(a.get("task_id") or "")
                a["task_name"] = (dyn or {}).get("name") or ""
                if not dyn or not _mf.sub_can_run_task(row.get("approved_by") or "", dyn):
                    stale = True
            # mcp_tool runs on the APPROVER's standing surface authority —
            # a demoted approver stales the whole approval (mirrors the
            # exec-time re-check).
            if has_mcp_tool and not _mf.sub_can_approve_surface(
                    row.get("approved_by") or "", row):
                stale = True
        can_approve = _can_approve_surface(row, u)
        if can_approve:
            for a in actions:
                if a.get("type") == "mcp_tool" and not a.get("mcp_available"):
                    can_approve = False
                if a.get("type") != "fire_task":
                    continue
                dyn = task_store.get_dynamic_task(a.get("task_id") or "")
                if "task_name" not in a:
                    a["task_name"] = (dyn or {}).get("name") or ""
                if not dyn or not _mf.user_can_run_task(u, dyn):
                    can_approve = False
        out.append({
            "id": row["id"],
            "slug": row["slug"],
            "title": row["title"],
            "scope": "personal" if row.get("username") else "shared",
            "pin_scope": ("chat" if row.get("scope_chat_id")
                          else "project" if row.get("scope_project_id")
                          else "standing"),
            "position": row["position"],
            "rel_path": row["rel_path"],
            "updated_at": row["updated_at"],
            "actions": actions,
            "actions_sig": task_store.actions_sig(row.get("actions") or "[]"),
            "actions_approved": approved and not stale,
            "approval_stale": stale,
            "can_approve": can_approve,
            "can_manage": _can_manage(row, u),
        })
    return out


@router.get("/v1/apps")
async def list_apps(
    agent: str,
    user: UserContext | None = Depends(get_current_user),
):
    """The viewer's merged app list: shared rows first, then their own
    personal rows (each by position; order[0] is the default tab). Standing
    rows only — chat/project Dock pins serve through
    ``GET /v1/chats/{chat_id}/pins``."""
    u = require_auth(user)
    require_agent_access(u, agent)

    def _load() -> list[dict]:
        return shape_app_rows(task_store.list_apps(agent, _viewer_username(u)), u)

    return {"apps": await asyncio.to_thread(_load)}


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
    is what delegates the run to every app viewer."""
    u = require_auth(user)
    row = await asyncio.to_thread(task_store.get_app, app_id)
    if not row or row.get("hidden") or not app_access(row, u):
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_approve_surface(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to approve this app's actions")

    def _check_targets() -> str:
        available_mcps: dict[str, str] | None = None
        for a in _mf.parse_actions(row):
            if a.get("type") == "mcp_tool":
                if available_mcps is None:
                    available_mcps = _mf.assigned_mcp_keys(row["agent"])
                if available_mcps.get(a.get("mcp") or "") != a.get("mcp"):
                    return f"action {a.get('id')!r}: its MCP is not available on this agent"
                continue
            if a.get("type") != "fire_task":
                continue
            err = _mf.check_task_target(
                a.get("task_id") or "", row["agent"], shared=not row.get("username"),
            )
            if err:
                return err
            dyn = task_store.get_dynamic_task(a.get("task_id") or "")
            if not _mf.user_can_run_task(u, dyn or {}):
                return f"action {a.get('id')!r}: you lack run authority for its task"
        return ""

    err = await asyncio.to_thread(_check_targets)
    if err:
        raise HTTPException(status_code=403, detail=err)
    ok = await asyncio.to_thread(task_store.approve_app_actions, app_id, req.sig, u.sub)
    if not ok:
        raise HTTPException(status_code=409, detail="The manifest changed — review it again")
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


def _check_fire_rate(app_id: str, action_id: str, sub: str,
                     args_key: str = "",
                     interval: float = _FIRE_MIN_INTERVAL_S) -> None:
    """Min-interval per (app, action[, args], user). ``args_key``
    distinguishes DIFFERENT parameter values of one declared action — a
    control panel often shares one schema-bound action across many widgets
    (one ``toggle`` action, entity in args), and keying without the args
    made pressing two different lights look like hammering one button.
    Same args stay limited (a toggle double-press must not fire twice)."""
    key = (app_id, f"{action_id}|{args_key}" if args_key else action_id, sub)
    now = time.monotonic()
    if now - _fire_rate.get(key, 0.0) < interval:
        raise HTTPException(status_code=429, detail="Too fast — try again in a moment")
    _fire_rate[key] = now
    if len(_fire_rate) > 1024:
        for k in [k for k, t in _fire_rate.items() if now - t > 300]:
            _fire_rate.pop(k, None)


@router.post("/v1/apps/{app_id}/actions/{action_id}")
async def run_app_action(
    app_id: str,
    action_id: str,
    req: ActionRequest | None = None,
    user: UserContext | None = Depends(get_current_user),
):
    """Execute a declared fire_task or mcp_tool action. send_prompt actions
    are delivered through the chat WS instead.

    Page args only ever pass through the action's user-approved
    ``args_schema``: a schema-less fire_task fires VERBATIM (args rejected —
    a page-controlled prompt_override would be prompt injection with full
    task authority); with a schema, validated args substitute into the task
    prompt. mcp_tool merges validated args UNDER the declared fixed_args and
    runs the one declared tool headlessly (no agent session, no LLM turn)."""
    u = require_auth(user)
    row = await asyncio.to_thread(task_store.get_app, app_id)
    if not row or row.get("hidden") or not app_access(row, u):
        raise HTTPException(status_code=404, detail="App not found")
    action = _mf.find_action(row, action_id)
    if action is None:
        raise HTTPException(status_code=404, detail="Unknown action")
    if action.get("type") == "send_prompt":
        raise HTTPException(status_code=400, detail="This action is delivered through the chat")
    if action.get("type") == "data_feed":
        raise HTTPException(status_code=400,
                            detail="Feeds are answered by the host page, not fired")
    if not task_store.app_actions_approved(row):
        raise HTTPException(status_code=409, detail="Actions not approved")
    args = req.args if req else None

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
        merged_json = json.dumps(merged, sort_keys=True, separators=(",", ":"))
        if len(merged_json.encode("utf-8")) > 8192:
            raise HTTPException(status_code=400, detail="Arguments too large")
        # Args-aware, shorter interval for direct tool calls: the schema
        # bounds every value and the headless in-flight guard + the tool's
        # own latency do the heavy limiting. Identical repeat calls (a
        # toggle double-press) still wait the full second.
        args_key = hashlib.sha256(merged_json.encode("utf-8")).hexdigest()[:16]
        _check_fire_rate(app_id, action_id, u.sub, args_key=args_key, interval=1.0)
        from services.apps import headless_exec
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
    _check_fire_rate(app_id, action_id, u.sub)

    from services.scheduler import scheduler
    task_def = scheduler._row_to_task(dyn)
    prompt_override = None
    if action.get("args_schema"):
        # Safe now: the values are schema-bounded (type/enum/length) and the
        # SCHEMA was what the user approved — never free-form page text.
        from services.scheduler.trigger_manager import _substitute_placeholders
        prompt_override = _substitute_placeholders(task_def.prompt or "", validated) or ""
        if len(prompt_override) > 8000:
            raise HTTPException(status_code=400, detail="Prompt too large after substitution")
    run_id = await scheduler.trigger_task_now(
        task_def, trigger_type="app_action",
        trigger_source=f"{row['slug']}:{action_id}",
        prompt_override=prompt_override,
    )
    logger.info(
        f"App action fired: app={row['slug']}, action={action_id}, "
        f"task={task_id}, by={u.sub[:16]}, run={run_id}"
    )
    return {"status": "ok", "run_id": run_id}


class OrderRequest(BaseModel):
    agent: str
    ids: list[str]


@router.put("/v1/apps/order")
async def reorder_apps(
    req: OrderRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Reorder the viewer's merged list. Positions renumber WITHIN each
    scope group; moving shared rows needs editor+ (viewers may still
    reorder their own personal rows — their shared subsequence must arrive
    unchanged)."""
    u = require_auth(user)
    require_agent_access(u, req.agent)

    def _apply() -> str:
        rows = {r["id"]: r for r in task_store.list_apps(req.agent, _viewer_username(u))}
        wanted = [rows[i] for i in req.ids if i in rows]
        if len(wanted) != len(rows):
            return "stale list — refresh and try again"
        shared_new = [r["id"] for r in wanted if not r["username"]]
        personal_new = [r["id"] for r in wanted if r["username"]]
        shared_cur = [r["id"] for r in task_store.list_apps(req.agent, "")]
        if shared_new != shared_cur and not (u.is_admin or u.can_edit_agent(req.agent)):
            return "editor role required to reorder shared apps"
        updates = [(i, pos) for pos, i in enumerate(shared_new)]
        updates += [(i, pos) for pos, i in enumerate(personal_new)]
        task_store.set_app_positions(updates)
        return ""

    err = await asyncio.to_thread(_apply)
    if err:
        raise HTTPException(status_code=409 if "stale" in err else 403, detail=err)
    return {"status": "ok"}


@router.delete("/v1/apps/{app_id}")
async def unpin_app(
    app_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """Dashboard-side unpin is SOFT: the row is hidden, not deleted — the
    workspace ``.html``, the actions manifest AND its approval all survive,
    so an agent ``pin_app(slug)`` restores the app exactly as approved.
    The agent-side unpin hook is the hard delete."""
    u = require_auth(user)
    row = await asyncio.to_thread(task_store.get_app, app_id)
    if not row or row.get("hidden") or not app_access(row, u):
        raise HTTPException(status_code=404, detail="App not found")
    if not _can_manage(row, u):
        raise HTTPException(status_code=403, detail="Not authorized to unpin this app")
    await asyncio.to_thread(task_store.set_app_hidden, app_id, True)
    return {"status": "ok"}
