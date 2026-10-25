"""A person's report of an agent's reply, and the admins' list of reports.

The stores ask an app whose chat with an AI is central to let a person report
an offensive reply from inside it (Google Play's AI-generated content policy,
Apple's guideline 1.2). The report goes to the admins of this install, who run
its agents and choose their providers; nothing goes to OtoDock.

A person reports in a chat they can read (``can_access_chat``), from the
dashboard only: an agent's session token never files one. The reply's text
is read from the chat's rows, never taken from the client, so a report
cannot put words in an agent's mouth. A reply's rows follow the dashboard's
history builder (``lib/messageBlocks.ts``, ``dbMessagesToDisplay``): any row
of a reply names it, by its first row.

The hourly cap and the notice gap are read and the row written in separate
statements: two reports sent at the same moment can pass the cap by one or
both notify. Accepted: a burst is bounded by the in-memory limiter first.
"""

import json
import logging
import re
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field

import config
from auth import rate_limiter
from auth.providers import (
    UserContext,
    get_current_user,
    require_admin,
    require_human,
    require_user,
)
from core.session.visibility import can_access_chat
from storage import database as task_store, db_reply_reports as reports
from storage.automation import notification_store
from storage.pg import run_db
from ws import wire_events as wire

logger = logging.getLogger("otodock.reply-reports")
router = APIRouter(dependencies=[Depends(require_user)])

#: The reasons a person picks from, in the dialog's order, with the words
#: the admins read.
REASONS: dict[str, str] = {
    "harmful": "Harmful or offensive",
    "misleading": "Wrong or misleading",
    "private": "Private information",
    "other": "Something else",
}
NOTE_MAX = 1000
REPLY_TEXT_MAX = 20_000
#: The notification source of a report.
SOURCE = "report"
HOURLY_CAP = 20
_HOUR_S = 3600
#: One notice per person in this window, measured from their last NOTIFIED
#: report; the next one says how many came in between. Every report is a row.
NOTICE_GAP_S = 600
#: The in-memory limiter's bucket (``config.RATE_LIMIT_RULES``): every
#: request, repeats included, before any row is read.
_LIMIT_BUCKET = "reply_report"
#: The admins' list: the open reports, or the dismissed history.
OPEN, DISMISSED = "open", "dismissed"
#: ``message_id`` and ``before_id`` are int4 in Postgres.
_ID_MAX = 2_147_483_647
#: The rows read each way from the named row.
ROWS_MAX = 2000

# Persisted row roles (the chat writer's).
_PERSON_ROW = "user"
_AGENT_ROW = "assistant"
# Events that open a bubble of their own in the history builder and start it
# (each renders a block); a delegate result is a bubble apart.
_HOSTING_OPENERS = frozenset({
    wire.PERSISTED_BG_NUDGE, wire.PERSISTED_BG_COMMAND_NUDGE,
    wire.ARTIFACT_INTERACTION, wire.APP_ACTION, wire.PERSISTED_SCHEDULE_WAKE,
})
# A review's nudge rows: a second kind right after the first joins its
# bubble (a run's review that names agents and commands stores both).
_NUDGES = frozenset({wire.PERSISTED_BG_NUDGE, wire.PERSISTED_BG_COMMAND_NUDGE})
# A web address's scheme: the inbox links every one (lib/linkify.ts).
_SCHEME_RE = re.compile(r"https?://", re.IGNORECASE)
# A person's row the history builder hides (``cleanUserMessageText`` in
# dashboard lib/transcriptCleanup.ts): stamps the platform prepends, a
# slash-command record, a /context report, a task notification. It does not
# end a reply there, so it does not end one here.
_PRELUDE_RE = re.compile(
    r"^\[(?:(?:Current time: |The user is looking at the app )[^\]\n]{1,200}"
    r"|Parallel activity \u2014 [^\n]{0,2000}?\(peek_session / get_task_result\)\."
    r'|Meeting "[^\n]{0,4000}? you have only this note\.)\](?:[ \t]*(?:\r?\n+|$)|[ \t]+)'
)
_REMINDER_RE = re.compile(r"<system-reminder>[\s\S]*?</system-reminder>")
_COMMAND_RE = re.compile(
    r"<(command-name|command-message|command-args|local-command-stdout)>[\s\S]*?</\1>")
_COMMAND_START_RE = re.compile(
    r"^<(command-name|command-message|command-args|local-command-stdout)>")


class ReportRequest(BaseModel):
    message_id: int = Field(ge=1, le=_ID_MAX)
    reason: str
    note: str = Field(default="", max_length=NOTE_MAX)


def _event_data(row: dict) -> dict:
    try:
        data = json.loads(row.get("event_data") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _is_result(row: dict) -> bool:
    return row.get("event_type") == wire.DELEGATE_RESULT


def _meeting_start(row: dict) -> bool:
    return (row.get("event_type") == wire.SYSTEM
            and _event_data(row).get("subtype") == wire.SUBTYPE_MEETING_TURN_START)


def _strip_preludes(text: str) -> str:
    while True:
        cut = _PRELUDE_RE.sub("", text, count=1)
        if cut == text:
            return text
        text = cut


def _hidden_person_row(row: dict) -> bool:
    """A person's row the history builder hides: nothing the person wrote
    survives the cleanup, and it carries no attachment."""
    data = _event_data(row)
    if data.get("images") or data.get("files"):
        return False
    raw = row.get("content") or ""
    text = _strip_preludes(raw)
    if text.lstrip().startswith("<system-reminder>"):
        text = _strip_preludes(_REMINDER_RE.sub("", text).lstrip())
    kept = text.strip()
    if not kept:
        return bool(raw.strip())
    if _COMMAND_START_RE.match(kept):
        return not _COMMAND_RE.sub("", kept).strip()
    if kept.startswith("## Context Usage") and "**Tokens:**" in kept:
        return True
    return kept.startswith("<task-notification>") and "</task-notification>" in kept


def _ends_reply(row: dict) -> bool:
    """A person's message the reader sees, or a delegate's result."""
    return (row["role"] == _PERSON_ROW and not _hidden_person_row(row)) or _is_result(row)


def _hosts_bubble(row: dict) -> bool:
    return (row.get("event_type") or "") in _HOSTING_OPENERS or _meeting_start(row)


def _is_nudge(row: dict) -> bool:
    return (row.get("event_type") or "") in _NUDGES


def _review_start(row: dict, earlier) -> dict:
    """An opener, or for a nudge row the first of the nudge rows right
    before it (``earlier`` newest first): the bubble they share."""
    if _is_nudge(row):
        for prev in earlier:
            if not _is_nudge(prev):
                break
            row = prev
    return row


def _speaker(row: dict) -> str:
    """The agent a row names as its speaker (a meeting, a delegate), or ''."""
    data = _event_data(row)
    named = data.get("agent_slug") or data.get("_meeting_agent")
    if not named and (_is_result(row) or _meeting_start(row)):
        named = data.get("agent")
    return str(named or "")


def first_row(named: dict, earlier: list[dict]) -> dict:
    """The first row of the reply ``named`` belongs to; ``earlier`` are the
    chat's rows before it, newest first."""
    if _is_result(named):
        return named
    if _hosts_bubble(named):
        return _review_start(named, earlier)
    start, speaker = named, _speaker(named)
    for i, row in enumerate(earlier):
        if _ends_reply(row):
            break
        if row["role"] == _PERSON_ROW:
            continue
        other = _speaker(row)
        if speaker and other and other != speaker:
            break
        if _hosts_bubble(row):
            return _review_start(row, earlier[i + 1:])
        start, speaker = row, speaker or other
    return start


def reply_text(first: dict, rows: list[dict]) -> tuple[str, str]:
    """The reply's text and speaker from its first row on (``rows`` start
    with it): the agent's rows, progress notes included, up to the next
    person's message, opening event, delegate result or speaker."""
    if _is_result(first):
        text, speaker = str(_event_data(first).get("output_text") or ""), _speaker(first)
    else:
        speaker, ended = _speaker(first), False
        lines_only = _is_nudge(first)
        parts: list[str] = []
        for row in rows:
            if row["id"] != first["id"]:
                if lines_only and _is_nudge(row):
                    continue
                lines_only = False
                if _ends_reply(row) or _hosts_bubble(row):
                    ended = True
                    break
                if row["role"] == _PERSON_ROW:
                    continue
                other = _speaker(row)
                if speaker and other and other != speaker:
                    ended = True
                    break
                speaker = speaker or other
            if row["role"] == _AGENT_ROW and row.get("content"):
                parts.append(row["content"])
        text = "\n\n".join(parts)
        if not ended and len(rows) >= ROWS_MAX:
            text += "…"
    if len(text) > REPLY_TEXT_MAX:
        text = text[:REPLY_TEXT_MAX - 1] + "…"
    return text, speaker


def _reply_time(first: dict) -> str:
    """The reply's time as its bubble header reads it ("21 Oct, 14:32"), in
    the platform's time zone; '' when the row's stamp does not parse."""
    try:
        at = datetime.fromisoformat(str(first.get("created_at") or ""))
    except ValueError:
        return ""
    if at.tzinfo is None:
        return ""
    at = at.astimezone(config.get_platform_tz())
    return f"{at.day} {at:%b}, {at:%H:%M}"


def _file(u: UserContext, chat_id: str, req: ReportRequest, note: str) -> dict:
    """One job: the checks, the reply's rows, the row. Raises the refusal."""
    chat = task_store.get_chat(chat_id)
    if not chat:
        raise HTTPException(status_code=404, detail="Chat not found")
    if not can_access_chat(u, chat):
        raise HTTPException(status_code=403, detail="Access denied")
    named = reports.message_in_chat(chat_id, req.message_id)
    if not named:
        raise HTTPException(status_code=404, detail="That message is not in this chat.")
    if named["role"] == _PERSON_ROW:
        raise HTTPException(status_code=400, detail="Only an agent's reply can be reported.")
    # A repeat names the first row the first report stored: no rows read.
    if reports.has_report(u.sub, named["id"]):
        return {"id": None}
    first = first_row(named, reports.rows_before(chat_id, named["id"], ROWS_MAX))
    if first["id"] != named["id"] and reports.has_report(u.sub, first["id"]):
        return {"id": None}
    if reports.count_since(u.sub, _HOUR_S) >= HOURLY_CAP:
        raise HTTPException(
            status_code=429, detail="You sent many reports in the last hour. Try again later.")
    text, speaker = reply_text(first, reports.rows_from(chat_id, first["id"], ROWS_MAX))
    if not text.strip():
        raise HTTPException(status_code=400, detail="This reply has no text to report.")
    agent = speaker or chat.get("agent") or ""
    due = reports.notice_due(u.sub, NOTICE_GAP_S)
    report_id = reports.add(u.sub, chat_id, first["id"], agent=agent, reason=req.reason,
                            note=note, reply_text=text)
    if report_id is None or not due:
        return {"id": report_id}
    more = reports.mark_notified(report_id)
    from services.notifications.notification_manager import agent_label
    name = notification_store.resolve_sub_to_display_name(u.sub) or u.name or "Someone"
    when = _reply_time(first)
    body = (f"{name} reported a reply of {agent_label(agent) or 'an agent'}"
            f"{f' from {when}' if when else ''}. Reason: {REASONS[req.reason]}.")
    if note:
        body += " A note came with it."
    if more:
        body += f" And {more} more since."
    return {"id": report_id, "agent": chat.get("agent") or "", "body": _SCHEME_RE.sub("", body)}


@router.post("/v1/chats/{chat_id}/reports")
async def report_reply(
    chat_id: str,
    req: ReportRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_human(user)
    if req.reason not in REASONS:
        raise HTTPException(status_code=422, detail="Pick one of the reasons.")
    allowed, retry_after = rate_limiter.hit(_LIMIT_BUCKET, u.sub)
    if not allowed:
        raise HTTPException(
            status_code=429, detail=f"Too many reports. Try again in {retry_after} seconds.")
    filed = await run_db(_file, u, chat_id, req, req.note.strip())
    if filed.get("body"):
        from services.notifications.notification_manager import fire_notification
        try:
            await fire_notification(
                title="A reply was reported", body=filed["body"], severity="warning",
                scope="admin", source=SOURCE, source_id=f"report:{filed['id']}",
                agent_slug=filed["agent"] or None, chat_id=chat_id,
            )
        except Exception:
            logger.exception("reply report %s: the admin notification failed", filed["id"])
    return {"status": "reported"}


@router.get("/v1/admin/reply-reports")
async def list_reply_reports(
    limit: int = Query(50, ge=1, le=100),
    before_id: int | None = Query(None, ge=1, le=_ID_MAX),
    state: Literal["open", "dismissed"] = Query(OPEN),
    user: UserContext | None = Depends(get_current_user),
):
    """The open reports, or the dismissed history; the open list also counts
    the history so the page's strip needs no second call."""
    require_admin(user)
    dismissed = state == DISMISSED

    def _job() -> tuple[list[dict], bool, dict[str, str], int | None]:
        rows, has_more = reports.list_page(limit, before_id, dismissed=dismissed)
        names = notification_store.resolve_subs_to_display_names(
            [r["reporter_sub"] for r in rows] + [r["dismissed_by"] for r in rows])
        return rows, has_more, names, None if dismissed else reports.count_dismissed()

    rows, has_more, names, count = await run_db(_job)
    body: dict = {
        "reports": [
            {**r, "created_at": r["created_at"].isoformat(),
             "reply_at": r["reply_at"] or "",
             "reporter_name": names.get(r["reporter_sub"]) or r["reporter_sub"][:12],
             "dismissed_at": r["dismissed_at"].isoformat() if r["dismissed_at"] else "",
             "dismissed_by_name": (names.get(r["dismissed_by"]) or r["dismissed_by"][:12]
                                   if r["dismissed_by"] else "")}
            for r in rows
        ],
        "has_more": has_more,
    }
    if count is not None:
        body["dismissed_count"] = count
    return body


async def _set_dismissed(user: UserContext | None, report_id: int, dismiss: bool) -> None:
    u = require_admin(user)
    if not await run_db(reports.set_dismissed, report_id, u.sub if dismiss else None):
        raise HTTPException(status_code=404, detail="Report not found")


@router.post("/v1/admin/reply-reports/{report_id}/dismiss")
async def dismiss_reply_report(
    report_id: int = Path(ge=1, le=_ID_MAX),
    user: UserContext | None = Depends(get_current_user),
):
    """Move a report to the page's dismissed history (nothing is deleted).
    Dismissing it again changes nothing."""
    await _set_dismissed(user, report_id, True)
    return {"status": DISMISSED}


@router.post("/v1/admin/reply-reports/{report_id}/restore")
async def restore_reply_report(
    report_id: int = Path(ge=1, le=_ID_MAX),
    user: UserContext | None = Depends(get_current_user),
):
    """Bring a dismissed report back to the open list (idempotent)."""
    await _set_dismissed(user, report_id, False)
    return {"status": OPEN}
