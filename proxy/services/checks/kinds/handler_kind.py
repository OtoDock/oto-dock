"""The handler kind (CHECKS.md "The handler kind"): an app's server holds
the logic and its settings. The platform POSTs the app's handler once,
synchronously (the verdict is needed within the turn), with a platform
claim of ``kind: check``, and reads the verdict from the 2xx body. The
app must be approved and declare the handler under the event
``check:<name>`` (so the approval card says it and an old proxy refuses
the manifest); the handler's platform methods are a handler's, for the
run's minute (``app_proxy.register_inflight_check``).
"""

from __future__ import annotations

import json
import logging
import time

from services.checks import kinds
from services.checks.render import Verdict, parse_verdict_json
from storage import db_apps

logger = logging.getLogger("checks")

HANDLER_TIMEOUT_S = 60.0
CLAIM_MARGIN_S = 30


def _error(reason: str, started: float) -> Verdict:
    return Verdict(section="handler", status="error", reason=reason,
                   duration_ms=int((time.monotonic() - started) * 1000))


def find_app(agent: str, owner: str, username: str, slug: str) -> dict | None:
    """The app the check names. A person's own check (``owner``): their
    personal app, else the shared one. An agent check: the agent's shared
    app, else the judged person's own copy (a template's per-member app) —
    a personal app never shadows the shared one an agent check names, so a
    member cannot answer a manager's check with an app of their own."""
    from storage import database as task_store
    scopes = [owner, ""] if owner else (["", username] if username else [""])
    for who in scopes:
        row = task_store.get_app_by_slug(agent, who, slug)
        if row:
            return row
    return None


async def run(check, target, changed: dict, *, round_no: int) -> Verdict:
    import asyncio
    import httpx
    from api.apps import manifest as _mf
    from api.apps.app_proxy import forward, register_inflight_check, unregister_inflight_check
    from services.apps import app_supervisor, app_tokens
    from storage import database as task_store
    started = time.monotonic()
    spec = check.doc.get("handler") or {}
    slug, handler = str(spec.get("app") or ""), str(spec.get("handler") or "")
    row = await asyncio.to_thread(find_app, target.agent, check.owner, target.username, slug)
    if not row or not db_apps.app_kind_of(row).may_serve:
        return _error(f"the app {slug!r} is not on this agent (a folder app with a server)", started)
    if not task_store.app_actions_approved(row):
        return _error(f"the app {slug!r} is waiting for approval", started)
    event = f"check:{check.name}"
    on_event = _mf.parse_handlers(row).get("on_event") or {}
    if event not in (on_event.get(handler) or []):
        return _error(f"the app {slug!r} does not declare handler {handler!r} for the event {event}",
                      started)
    try:
        inst = await app_supervisor.ensure_up(row)
    except app_supervisor.AppUnavailable as e:
        return _error(f"the app's server is not up: {e.reason}", started)
    if inst.state == app_supervisor.STATIC:
        return _error("the app has no server", started)
    run_id = f"check-{check.name}-{int(started * 1000)}"
    claim = app_tokens.mint(row["id"], app_tokens.PURPOSE_CALLER, {
        "principal": "platform", "kind": "check", "sub": "", "username": "", "role": "app",
        "handler": handler, "delivery": run_id, "event": event, "check": check.name,
        "trigger_id": "", "external": False,
    }, int(HANDLER_TIMEOUT_S) + CLAIM_MARGIN_S)
    body = json.dumps({
        "event": event, "handler": handler, "delivery_id": run_id, "attempt": 1,
        "payload": {**changed, "check": check.name, "round": round_no},
    }, default=str).encode("utf-8")
    headers = [("Content-Type", "application/json"), ("X-OtoDock-Viewer", claim),
               ("X-OtoDock-Basis", "platform"), ("X-OtoDock-Delivery-Id", run_id)]
    register_inflight_check(run_id, row["id"], handler, event, ttl_s=int(HANDLER_TIMEOUT_S) + CLAIM_MARGIN_S)
    app_supervisor.touch(row["id"])
    try:
        upstream = await forward(inst, "POST", f"/_handler/{handler}", headers, body,
                                 timeout=HANDLER_TIMEOUT_S)
        text = (await upstream.aread())[:64 * 1024].decode("utf-8", "replace")
        code = upstream.status_code
        await upstream.aclose()
    except httpx.HTTPError as e:
        return _error(f"the app's server did not answer: {e.__class__.__name__}", started)
    finally:
        unregister_inflight_check(run_id)
    if code >= 300:
        return _error(f"the handler answered {code}: {text[:200]}", started)
    try:
        obj = json.loads(text) if text.strip() else None
    except ValueError:
        obj = None
    parsed = parse_verdict_json(obj)
    if parsed is None:
        return _error("the handler's answer is not a verdict", started)
    passed, score, findings, summary = parsed
    return Verdict(section="handler", status="pass" if passed else "fail", passed=passed,
                   score=score, findings=findings, summary=summary,
                   duration_ms=int((time.monotonic() - started) * 1000))


kinds.register("handler", run)
