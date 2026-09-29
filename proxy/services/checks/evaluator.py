"""The one ``turn_end`` handler (CHECKS.md "One handler, filtered by
applies"). At the end of a judged session's turn it resolves the session,
finds the checks attached to it — the agent's mandatory ones by
``applies``, the refs on the chat row — builds the changed set from the
turn's tool record, evaluates each check whose condition matches (the
present sections in order, first failure wins), records every verdict
(the store and the ``check_verdict`` chat event) and, while a failing check
has rounds left, hands the agent the findings as the continue reason.
Never a meeting, a phone call, an app's headless call, an external caller
or a check session itself. Everything is bounded and every outcome is a
visible verdict — a check that could not run is ``error``, never a silent
pass.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time

from core.session import session_events
from core.session.visibility import is_synthetic_owner
from services.checks import changed_set as cs
from services.checks import documents, kinds, render
from services.checks.render import Verdict
from storage.checks import db_checks
from storage.pg import run_db
from core.session import session_kind
from services.scheduler import task_kinds
from auth import roles
from ws import wire_events as wire
from core import layout

logger = logging.getLogger("checks")

# Beyond the sections' own timeouts, the whole evaluation of one check.
EVALUATION_MARGIN_S = 60
_installed = False


def install() -> None:
    """Register the handler once (startup)."""
    global _installed
    if _installed:
        return
    kinds.install_all()
    session_events.register_turn_end_handler(on_turn_end)
    _installed = True


# ── the session ─────────────────────────────────────────────────────────────


async def resolve_target(session_id: str, engine: str = "") -> cs.Target | None:
    """The judged session, or None when checks never apply to it."""
    from core.session.session_state import (
        _sessions, get_meeting_session_info, get_session_client_type, get_session_security,
    )
    from storage import database as task_store
    entry = _sessions.get(session_id) or {}
    if entry.get("check"):
        return None                       # a judge is never judged
    sec = get_session_security(session_id)
    if sec is None:
        return None
    if getattr(sec, "principal", "user") == "external":
        return None
    client_type = get_session_client_type(session_id)
    if not session_kind.judged(client_type):
        return None
    if get_meeting_session_info(session_id):
        return None
    chat = await asyncio.to_thread(task_store.get_chat_by_session, session_id)
    if not chat:
        return None
    run = None
    row_kind = session_kind.of_chat(chat)
    if client_type == session_kind.TASK.name or row_kind is session_kind.TASK or entry.get("is_task"):
        run = await asyncio.to_thread(task_store.get_run_for_chat, chat.get("id") or "")
        run_type = (run or {}).get("task_type") or ""
        if run_type == task_kinds.RUN_CHECK:
            return None
        kind = ("delegations" if (run_type == task_kinds.RUN_DELEGATE or chat.get("delegate_role") == "worker")
                else "tasks")
    elif client_type in (session_kind.DASHBOARD.name, "") and row_kind is session_kind.DASHBOARD:
        kind = "chats"
    else:
        return None
    username = getattr(sec, "mount_username", "") or getattr(sec, "username", "") or ""
    scope = getattr(sec, "session_scope", "user") or "user"
    user_sub = ""
    owner = chat.get("user_sub") or ""
    if owner and not is_synthetic_owner(owner):
        user_sub = owner
    work_cwd = getattr(sec, "work_cwd", "") or chat.get("work_cwd") or ""
    cwd = work_cwd or (layout.virtual_user_root(username) if (username and scope == "user") else layout.V_WORKSPACE)
    return cs.Target(
        session_id=session_id, kind=kind, agent=getattr(sec, "agent", "") or chat.get("agent") or "",
        chat=chat, username=username, user_sub=user_sub, role=getattr(sec, "role", roles.VIEWER) or roles.VIEWER,
        scope=scope, engine=engine or chat.get("execution_path") or "", client_type=client_type,
        security=sec, placement=sec.placement, cwd=cwd, work_cwd=work_cwd, run=run,
    )


def attached_checks(target: cs.Target) -> list[documents.CheckDoc]:
    """Mandatory agent checks that apply to this kind of session, then the
    refs attached to the chat (a task's copied in at run start); deduped by
    ref; broken documents skipped (their problems show on the Checks page)."""
    out: list[documents.CheckDoc] = []
    seen: set[str] = set()
    for it in documents.load_checks(target.agent, ""):
        if it.held:
            # A mandatory check whose file broke or went: judged by its last
            # valid document's applies and condition, reported as an error
            # (``on_turn_end``) — breaking the file never switches it off.
            held = dataclasses.replace(it, doc=it.held)
            if target.kind in held.applies:
                out.append(held)
                seen.add(it.ref)
            continue
        if it.mandatory and target.kind in it.applies and not it.problems:
            out.append(it)
            seen.add(it.ref)
    try:
        refs = json.loads(target.chat.get("checks") or "[]")
    except ValueError:
        refs = []
    for ref in refs if isinstance(refs, list) else []:
        if not isinstance(ref, str):
            continue
        it = documents.resolve_ref(target.agent, ref, target.username)
        if it is None or it.ref in seen or target.kind not in it.applies:
            continue
        out.append(it)
        seen.add(it.ref)
    return out


def _last_user_message(chat_id: str) -> str:
    from storage import database as task_store
    for row in reversed(task_store.get_chat_messages(chat_id, limit=60)):
        if row.get("role") == "user" and (row.get("content") or "").strip():
            return row["content"]
    return ""


# ── the evaluation ──────────────────────────────────────────────────────────


async def evaluate(check: documents.CheckDoc, target: cs.Target, changed: dict, *,
                   round_no: int, started: float) -> Verdict:
    """The present sections in order; the first failure is the verdict; an
    error or a skip stops there too."""
    last: Verdict | None = None
    for section in check.sections:
        if session_events.user_message_since(target.session_id, started):
            return Verdict(section=section, status="skipped",
                           reason="the person sent a new message")
        runner = kinds.RUNNERS.get(section)
        if runner is None:
            return Verdict(section=section, status="error",
                           reason=f"the {section} kind is not available on this platform")
        try:
            v = await runner(check, target, changed, round_no=round_no)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — the reason is the verdict
            logger.exception("check %s/%s: the %s section failed", target.agent, check.name, section)
            v = Verdict(section=section, status="error", reason=f"{section}: {str(e)[:300]}")
        last = v
        if v.status != "pass":
            return v
    return last or Verdict(section="", status="error", reason="the check has no sections")


def _budget_s(check: documents.CheckDoc) -> float:
    total = EVALUATION_MARGIN_S
    if check.doc.get("script"):
        total += int(check.doc["script"].get("timeout") or 0)
    if check.doc.get("judge"):
        from services.checks.kinds import judge_kind
        total += judge_kind.wait_budget_s(int(check.doc["judge"].get("timeout") or 0))
    if check.doc.get("handler"):
        total += 60
    return float(total)


async def record(check: documents.CheckDoc, target: cs.Target, v: Verdict, *,
                 round_no: int) -> Verdict:
    """The verdict store row and the chat card; the id lands on the verdict."""
    try:
        row = await run_db(
            db_checks.insert_verdict, agent=target.agent, owner=check.owner,
            check_name=check.name, section=v.section, status=v.status, passed=v.passed,
            score=v.score, findings=v.findings, summary=v.summary, reason=v.reason,
            session_id=target.session_id, chat_id=target.chat_id,
            run_id=str((target.run or {}).get("id") or ""), judge_run_id=v.judge_run_id,
            user_sub=target.user_sub, round_no=round_no, ran_on=v.ran_on,
            engine=v.engine or target.engine, model=v.model or target.model,
            cost_usd=v.cost_usd, duration_ms=v.duration_ms, script_sha256=v.script_sha256,
        )
        v.verdict_id = row["id"]
        try:
            from api.apps import catalog
            catalog.verdict_landed(row, target.agent, target.user_sub)
        except Exception:
            logger.debug("check %s: the feed delta was not emitted", check.name, exc_info=True)
    except Exception:
        logger.exception("check %s/%s: the verdict could not be stored", target.agent, check.name)
    event = render.card_event(check.name, v, round_no=round_no, rounds=check.rounds, ref=check.ref)
    try:
        from core.events.common_events import CHECK_VERDICT, CommonEvent
        from core.session.session_state import inject_pump_event, push_pump_event
        from storage import database as task_store
        # In order with the turn's blocks when the chat's pump is live (it
        # forwards the frame, keeps the live state whole and persists the
        # row); a row plus a best-effort frame when no pump holds the chat.
        if not inject_pump_event(target.chat_id, CommonEvent(type=CHECK_VERDICT, data=dict(event))):
            await asyncio.to_thread(task_store.add_chat_message, target.chat_id, "event", "",
                                    event_type=wire.CHECK_VERDICT, event_data=render.card_event_json(event))
            push_pump_event(target.chat_id, event)
        if target.user_sub:
            from services.notifications import notification_manager
            notification_manager.broadcast_chat_rows(target.user_sub, target.chat_id, target.agent)
    except Exception:
        logger.warning("check %s: the chat card could not be placed", check.name, exc_info=True)
    return v


def final_failure(chat_id: str, since_iso: str) -> str | None:
    """The report of a check that did not pass in a run: for every check
    judged on the chat since ``since_iso``, its newest verdict; a ``fail``
    among them is the run's failure (a later pass on the same check clears
    it). None when every check passed, erred or was skipped."""
    from storage.pg import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (check_name) check_name, status, round, summary, findings "
            "FROM check_verdicts WHERE chat_id=%s AND created_at>=%s "
            "ORDER BY check_name, created_at DESC", (chat_id, since_iso or "")).fetchall()
    failed = [dict(r) for r in rows if r["status"] == "fail"]
    if not failed:
        return None
    parts = []
    for f in failed:
        summary = (f.get("summary") or "").strip()
        parts.append(f"check {f['check_name']} did not pass after {int(f.get('round') or 1)} round"
                     f"{'s' if int(f.get('round') or 1) != 1 else ''}"
                     + (f": {summary[:300]}" if summary else ""))
    return "; ".join(parts)


def latest_failure(chat_id: str, since_iso: str = "") -> dict | None:
    """The newest failing verdict on a chat whose check has no later pass —
    what a delegation's hand-back attaches (CHECKS.md). ``since_iso`` (the
    run's start) keeps a reused worker chat's earlier runs out."""
    from storage.pg import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (check_name) check_name, status, round, summary, findings, "
            "ran_on, created_at FROM check_verdicts WHERE chat_id=%s AND created_at>=%s "
            "ORDER BY check_name, created_at DESC", (chat_id, since_iso or "")).fetchall()
    failed = sorted((dict(r) for r in rows if r["status"] == "fail"),
                    key=lambda r: r["created_at"], reverse=True)
    if not failed:
        return None
    f = failed[0]
    try:
        findings = json.loads(f.get("findings") or "[]")
    except ValueError:
        findings = []
    return {"check": f["check_name"], "round": int(f.get("round") or 1),
            "summary": f.get("summary") or "", "findings": findings[:6], "ran_on": f.get("ran_on") or ""}


async def run_by_hand(session_id: str, ref: str, *, engine: str = "") -> dict:
    """``run_check``: the named check on the calling session's chat, now,
    whatever its condition, over the turn's record as it stands and the
    chat's last exchange. The verdict is recorded and returned."""
    from storage import database as task_store
    target = await resolve_target(session_id, engine)
    if target is None:
        raise LookupError("this session is not one a check judges (a chat, a task or a delegation)")
    check = await asyncio.to_thread(documents.resolve_ref, target.agent, ref, target.username)
    if check is None:
        raise LookupError(f"no check named {ref!r} on this agent (or among your own)")
    records = session_events.tool_records(session_id)
    request = await asyncio.to_thread(_last_user_message, target.chat_id)

    def _last_assistant() -> str:
        for row in reversed(task_store.get_chat_messages(target.chat_id, limit=60)):
            if row.get("role") == "assistant" and (row.get("content") or "").strip():
                return row["content"]
        return ""
    result = await asyncio.to_thread(_last_assistant)
    started = time.monotonic()
    round_no = int(session_events.rounds(session_id)) + 1
    changed = await cs.build(target, records, check_name=check.name, round_no=round_no,
                             request=request, result=result)
    try:
        v = await asyncio.wait_for(
            evaluate(check, target, changed, round_no=round_no, started=started),
            timeout=_budget_s(check))
    except asyncio.TimeoutError:
        v = Verdict(section=check.sections[0] if check.sections else "", status="error",
                    reason=f"the check did not finish within {int(_budget_s(check))} s")
    v.duration_ms = v.duration_ms or int((time.monotonic() - started) * 1000)
    await record(check, target, v, round_no=round_no)
    return {"check": check.name, "ref": check.ref, "section": v.section, "status": v.status,
            "pass": v.passed, "score": v.score, "summary": v.summary or v.reason,
            "findings": v.findings, "ran_on": v.ran_on, "cost_usd": v.cost_usd,
            "duration_ms": v.duration_ms, "verdict_id": v.verdict_id,
            "text": render.render_reason(check.name, v, round_no=round_no, rounds=check.rounds,
                                         machine_name=target.place_name)
            if v.status == "fail" else ""}


def _announce_turn(target: cs.Target, ctx: session_events.TurnEndContext, verdicts: list[str]) -> None:
    """``turn_finished`` on the sessions feed, for a chat's turn (CHECKS.md
    "The catalog"); nothing costs when no app of the agent subscribes."""
    if target.kind != "chats":
        return
    try:
        from api.apps import catalog
        catalog.chat_turn_finished(target.chat, {
            "session_id": target.session_id, "engine": ctx.engine or target.engine,
            "rounds": int(ctx.rounds), "verdicts": verdicts, "username": target.username,
        })
    except Exception:
        logger.debug("turn_finished was not emitted", exc_info=True)


async def on_turn_end(ctx: session_events.TurnEndContext):
    """The registered handler."""
    target = await resolve_target(ctx.session_id, ctx.engine)
    if target is None:
        return None
    checks = await asyncio.to_thread(attached_checks, target)
    if not checks:
        _announce_turn(target, ctx, [])
        return None
    started = time.monotonic()
    round_no = int(ctx.rounds) + 1
    records = session_events.tool_records(ctx.session_id)
    request = await asyncio.to_thread(_last_user_message, target.chat_id)
    verdict_out = None
    judged: list[str] = []
    for check in checks:
        changed = await cs.build(target, records, check_name=check.name, round_no=round_no,
                                 request=request, result=ctx.last_message)
        if not cs.condition_matches(check.doc.get("condition") or {}, changed):
            continue
        if check.problems:
            v = Verdict(section="", status="error",
                        reason=f"the mandatory check cannot run: {check.problems[0]}; a manager "
                               "fixes or removes it on the agent's Checks page")
        else:
            try:
                v = await asyncio.wait_for(
                    evaluate(check, target, changed, round_no=round_no, started=started),
                    timeout=_budget_s(check))
            except asyncio.TimeoutError:
                v = Verdict(section=check.sections[0] if check.sections else "", status="error",
                            reason=f"the check did not finish within {int(_budget_s(check))} s")
        v.duration_ms = v.duration_ms or int((time.monotonic() - started) * 1000)
        await record(check, target, v, round_no=round_no)
        logger.info("check %s/%s round %d on %s: %s (%s)", target.agent, check.name, round_no,
                    target.chat_id[:8], v.status, v.summary or v.reason)
        judged.append(check.name)
        if v.status == "fail" and verdict_out is None and ctx.rounds < check.rounds:
            verdict_out = session_events.continue_with(render.render_reason(
                check.name, v, round_no=round_no, rounds=check.rounds,
                machine_name=target.place_name))
            break
    if verdict_out is None:
        _announce_turn(target, ctx, judged)
    return verdict_out
