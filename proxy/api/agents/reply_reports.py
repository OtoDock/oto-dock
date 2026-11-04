"""A person's report of an agent's reply, and the admins' list of reports.

The stores ask an app whose chat with an AI is central to let a person report
an offensive reply from inside it (Google Play's AI-generated content policy,
Apple's guideline 1.2). The report goes to the admins of this install, who run
its agents and choose their providers; nothing goes to OtoDock.

A person reports in a chat they can read (``can_access_chat``), or from a
shared copy of a chat they were given (the share decides), from the
dashboard only: an agent's session token never files one. The reply's text
is read from the chat's rows (or the copy's, for a row written again since),
never taken from the client, so a report cannot put words in an agent's
mouth. A reply with no text (tools, a card, images, an app) is described one
line per part. A reply's rows follow the dashboard's history builder
(``lib/messageBlocks.ts``, ``dbMessagesToDisplay``): any row of a reply
names it, by its first row.

An admin marks a report handled, dismisses it, opens it again or deletes
it. A report outlives its chat and goes a year after it was filed
(``db_reply_reports.KEEP_DAYS``, the retention sweep).

The hourly cap and the notice gap are read and the row written in separate
statements: two reports sent at the same moment can pass the cap by one or
both notify. Accepted: a burst is bounded by the in-memory limiter first.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from psycopg import errors as pg_errors
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
from core.session import session_kind
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
#: The admins' two lists: the open reports, and the closed ones (handled and
#: dismissed together).
CLOSED = "closed"
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


def _tool_name(name: str) -> str:
    """A tool as a person reads it: an MCP tool (``mcp``, its server and its
    tool, joined by double underscores) by its name and server."""
    parts = name.split("__")
    if len(parts) == 1:
        return name
    return f"{parts[2]} from {parts[1]}" if len(parts) == 3 and all(parts) else ""


def describe(event_type: str, data: dict) -> str:
    """One line for a part of a reply the person saw that is not text, for a
    reply with none: a tool, a card, images, an app, a file, a clip, a link,
    a document. '' for what a reply shows no one (thinking, the platform's
    own rows). The chat's rows and a shared copy's blocks carry the same
    fields."""
    if event_type == wire.PERSISTED_TOOL:
        name = _tool_name(str(data.get("name") or ""))
        return f"Used {name}." if name else ""
    if event_type == wire.SETTINGS_CARD:
        label = str(data.get("label") or data.get("setting") or "")
        return f"Showed the setting card \u201c{label}\u201d." if label else "Showed a setting card."
    if event_type in (wire.ACTION_CARD, wire.LIST_CARD):
        title = str(data.get("title") or "")
        return f"Showed the card \u201c{title}\u201d." if title else "Showed a card."
    if event_type == wire.IMAGES:
        images = data.get("images")
        n = len(images) if isinstance(images, list) else 0
        return "" if not n else "Showed an image." if n == 1 else f"Showed {n} images."
    if event_type == wire.UI:
        title = str(data.get("title") or "")
        return f"Showed the app \u201c{title}\u201d." if title else "Showed an app."
    if event_type in (wire.FILE, wire.DOCUMENT_PREVIEW):
        name = str(data.get("filename") or "")
        what = "the document" if event_type == wire.DOCUMENT_PREVIEW else "the file"
        return f"Showed {what} {name}." if name else f"Showed {what.replace('the ', 'a ')}."
    if event_type == wire.VIDEO:
        return "Showed a video."
    if event_type == wire.AUDIO:
        return "Played an audio clip."
    if event_type == wire.URL:
        # The host alone: a report outlives its chat by a year, and a path or
        # a query can carry a token (a signed address, a share link's).
        host = urlsplit(str(data.get("url") or "")).hostname or ""
        return f"Sent a link to {host}." if host else "Sent a link."
    return ""


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
    person's message, opening event, delegate result or speaker. A reply
    with no text is described, one line per part it showed."""
    if _is_result(first):
        text, speaker = str(_event_data(first).get("output_text") or ""), _speaker(first)
    else:
        speaker, ended = _speaker(first), False
        lines_only = _is_nudge(first)
        parts: list[str] = []
        shown: list[str] = []
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
            elif row["role"] != _AGENT_ROW and row["role"] != _PERSON_ROW:
                line = describe(row.get("event_type") or "", _event_data(row))
                if line:
                    shown.append(line)
        text = "\n\n".join(parts) or "\n".join(shown)
        if text and not ended and len(rows) >= ROWS_MAX:
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


def _chat_href(chat: dict, message_id: int | None) -> str:
    """The chat as a report's notice opens it: at the reply when it names
    one (``?message=``), a task run's chat in its own view (``?tasks=1``)."""
    query = ["tasks=1"] if session_kind.is_task_chat_id(chat["id"]) else []
    if message_id:
        query.append(f"message={message_id}")
    return f"/chat/{chat.get('agent') or ''}/{chat['id']}" + (f"?{'&'.join(query)}" if query else "")


def _store(u: UserContext, chat: dict, message_id: int, text: str, speaker: str, when: str,
           req: ReportRequest, note: str, *, anchored: bool = True) -> dict:
    """The row, and the notice it is due: the reply named by ``message_id``,
    its text already read. Raises the refusal."""
    if not text.strip():
        raise HTTPException(status_code=400, detail="This reply has no text to report.")
    agent = speaker or chat.get("agent") or ""
    due = reports.notice_due(u.sub, NOTICE_GAP_S)
    try:
        report_id = reports.add(u.sub, chat["id"], message_id, agent=agent, reason=req.reason,
                                note=note, reply_text=text, chat_title=chat.get("title") or "")
    except pg_errors.ForeignKeyViolation:
        # The chat was deleted after it was read.
        raise HTTPException(status_code=404, detail="Chat not found") from None
    if report_id is None or not due:
        return {"id": report_id, "message_id": message_id}
    more = reports.mark_notified(report_id)
    from services.notifications.notification_manager import agent_label
    name = notification_store.resolve_sub_to_display_name(u.sub) or u.name or "Someone"
    body = (f"{name} reported a reply of {agent_label(agent) or 'an agent'}"
            f"{f' from {when}' if when else ''}. Reason: {REASONS[req.reason]}.")
    if note:
        body += " A note came with it."
    if more:
        body += f" And {more} more since."
    return {"id": report_id, "message_id": message_id, "agent": chat.get("agent") or "",
            "chat_id": chat["id"], "body": _SCHEME_RE.sub("", body),
            "href": _chat_href(chat, message_id if anchored else None)}


def _file_named(u: UserContext, chat: dict, named: dict, req: ReportRequest, note: str) -> dict:
    """The reply ``named`` (any of its rows) belongs to, in a chat the person
    may report in. Raises the refusal."""
    if named["role"] == _PERSON_ROW:
        raise HTTPException(status_code=400, detail="Only an agent's reply can be reported.")
    # A repeat names the first row the first report stored: no rows read.
    if reports.has_report(u.sub, named["id"]):
        return {"id": None, "message_id": named["id"]}
    first = first_row(named, reports.rows_before(chat["id"], named["id"], ROWS_MAX))
    if first["id"] != named["id"] and reports.has_report(u.sub, first["id"]):
        return {"id": None, "message_id": first["id"]}
    if reports.count_since(u.sub, _HOUR_S) >= HOURLY_CAP:
        raise HTTPException(
            status_code=429, detail="You sent many reports in the last hour. Try again later.")
    text, speaker = reply_text(first, reports.rows_from(chat["id"], first["id"], ROWS_MAX))
    return _store(u, chat, first["id"], text, speaker, _reply_time(first), req, note)


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
    return _file_named(u, chat, named, req, note)


async def _notify(filed: dict) -> None:
    if not filed.get("body"):
        return
    from services.notifications.notification_manager import fire_notification
    try:
        await fire_notification(
            title="A reply was reported", body=filed["body"], severity="warning",
            scope="admin", source=SOURCE, source_id=f"report:{filed['id']}",
            agent_slug=filed["agent"] or None, chat_id=filed["chat_id"], href=filed["href"],
        )
    except Exception:
        logger.exception("reply report %s: the admin notification failed", filed["id"])


def _check_request(u: UserContext, req: ReportRequest) -> None:
    """The reason, then the in-memory limiter, before any row is read."""
    if req.reason not in REASONS:
        raise HTTPException(status_code=422, detail="Pick one of the reasons.")
    allowed, retry_after = rate_limiter.hit(_LIMIT_BUCKET, u.sub)
    if not allowed:
        raise HTTPException(
            status_code=429, detail=f"Too many reports. Try again in {retry_after} seconds.")


@router.post("/v1/chats/{chat_id}/reports")
async def report_reply(
    chat_id: str,
    req: ReportRequest,
    user: UserContext | None = Depends(get_current_user),
):
    u = require_human(user)
    _check_request(u, req)
    filed = await run_db(_file, u, chat_id, req, req.note.strip())
    await _notify(filed)
    return {"status": "reported", "message_id": filed["message_id"]}


@router.get("/v1/chats/{chat_id}/reports/mine")
async def my_reported_replies(
    chat_id: str,
    user: UserContext | None = Depends(get_current_user),
):
    """The first rows of the replies this person reported in a chat they can
    read, so the flag stays "Reported" across a reload."""
    u = require_human(user)

    def _job() -> list[int]:
        chat = task_store.get_chat(chat_id)
        if not chat:
            raise HTTPException(status_code=404, detail="Chat not found")
        if not can_access_chat(u, chat):
            raise HTTPException(status_code=403, detail="Access denied")
        return reports.reported_in(u.sub, chat_id)

    return {"message_ids": await run_db(_job)}


def _block_data(block: dict) -> dict:
    """A shared copy's block's fields: an artifact's or a card's ``data``, a
    tool block's ``event_data`` as written."""
    data = block.get("data")
    if isinstance(data, dict):
        return data
    try:
        data = json.loads(block.get("event_data") or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def copy_reply_text(messages: list, start: int) -> str:
    """The reply a shared copy's block starts, as ``reply_text`` composes one
    from the chat's rows: the agent's blocks from ``start`` up to the next
    person's message (a thinking block, which keeps no row, goes on with
    it), their text, else one described line for each part."""
    parts: list[str] = []
    shown: list[str] = []
    for block in messages[start:]:
        if not isinstance(block, dict) or block.get("role") == _PERSON_ROW:
            break
        if block.get("role") == _AGENT_ROW:
            if block.get("content"):
                parts.append(str(block["content"]))
            continue
        line = describe(str(block.get("event_type") or ""), _block_data(block))
        if line:
            shown.append(line)
    return "\n\n".join(parts) or "\n".join(shown)


def _file_shared(u: UserContext, share: dict, doc: dict, start: int,
                 req: ReportRequest, note: str) -> dict:
    """A report from a shared copy: the chat's own reply while its row is
    there, else the copy's words for the reply that block starts (the row
    was written again since). A copy dies with its chat, so the chat is
    there."""
    chat = task_store.get_chat(share.get("chat_id") or "")
    if not chat:
        raise HTTPException(status_code=404, detail="Share not found")
    named = reports.message_in_chat(chat["id"], req.message_id)
    if named and named["role"] != _PERSON_ROW:
        return _file_named(u, chat, named, req, note)
    if reports.has_report(u.sub, req.message_id):
        return {"id": None, "message_id": req.message_id}
    if reports.count_since(u.sub, _HOUR_S) >= HOURLY_CAP:
        raise HTTPException(
            status_code=429, detail="You sent many reports in the last hour. Try again later.")
    text = copy_reply_text(doc.get("messages") or [], start)
    if len(text) > REPLY_TEXT_MAX:
        text = text[:REPLY_TEXT_MAX - 1] + "…"
    return _store(u, chat, req.message_id, text, str(doc.get("agent") or ""), "", req, note,
                  anchored=False)


@router.post("/v1/shares/{share_id}/reports")
async def report_shared_reply(
    share_id: str,
    req: ReportRequest,
    user: UserContext | None = Depends(get_current_user),
):
    """Report an agent's reply from a shared copy of a chat (``/shared/<id>``):
    the share decides who may (its reader, its creator, an admin), never the
    chat. The block is the copy's; nothing the reader learns depends on the
    live chat."""
    u = require_human(user)
    _check_request(u, req)
    from api.sharing.shares import _load_snapshot_share
    from services.sharing import chat_snapshot

    def _load() -> tuple[dict, dict]:
        share = _load_snapshot_share(u, share_id)
        doc = chat_snapshot.load(share)
        if not doc:
            raise HTTPException(status_code=404, detail="Share not found")
        return share, doc

    share, doc = await asyncio.to_thread(_load)
    messages = doc.get("messages") or []
    start = next((i for i, m in enumerate(messages)
                  if isinstance(m, dict) and m.get("id") == req.message_id), None)
    if start is None:
        raise HTTPException(status_code=404, detail="That message is not in this shared chat.")
    if messages[start].get("role") == _PERSON_ROW:
        raise HTTPException(status_code=400, detail="Only an agent's reply can be reported.")
    filed = await run_db(_file_shared, u, share, doc, start, req, req.note.strip())
    await _notify(filed)
    # The copy's own row: the live reply's first row may be one the copy
    # does not carry, and the reader may not open the chat.
    return {"status": "reported", "message_id": req.message_id}


@router.get("/v1/admin/reply-reports")
async def list_reply_reports(
    limit: int = Query(50, ge=1, le=100),
    before_id: int | None = Query(None, ge=1, le=_ID_MAX),
    state: Literal["open", "closed"] = Query(reports.OPEN),
    user: UserContext | None = Depends(get_current_user),
):
    """The open reports, or the closed ones (handled and dismissed); the open
    list also counts the closed so the page's strip needs no second call."""
    require_admin(user)
    closed = state == CLOSED

    def _job() -> tuple[list[dict], bool, dict[str, str], int | None]:
        rows, has_more = reports.list_page(limit, before_id, closed=closed)
        names = notification_store.resolve_subs_to_display_names(
            [r["reporter_sub"] for r in rows] + [r["closed_by"] for r in rows if r["closed_by"]])
        return rows, has_more, names, None if closed else reports.count_closed()

    rows, has_more, names, count = await run_db(_job)
    body: dict = {
        "reports": [
            {"id": r["id"], "created_at": r["created_at"].isoformat(),
             "reporter_sub": r["reporter_sub"],
             "reporter_name": names.get(r["reporter_sub"]) or r["reporter_sub"][:12],
             "chat_id": r["chat_id"] or "", "chat_title": r["chat_title"] or "",
             "chat_agent": r["chat_agent"] or "", "message_id": r["message_id"],
             "reply_at": r["reply_at"] or "", "agent": r["agent"], "reason": r["reason"],
             "note": r["note"], "reply_text": r["reply_text"], "state": r["state"],
             "closed_at": r["closed_at"].isoformat() if r["closed_at"] else "",
             "closed_by": r["closed_by"] or "",
             "closed_by_name": (names.get(r["closed_by"]) or r["closed_by"][:12]
                                if r["closed_by"] else "")}
            for r in rows
        ],
        "has_more": has_more,
    }
    if count is not None:
        body["closed_count"] = count
    return body


async def _set_state(user: UserContext | None, report_id: int, state: str) -> dict:
    u = require_admin(user)
    now = await run_db(reports.set_state, report_id, state, u.sub)
    if now is None:
        raise HTTPException(status_code=404, detail="Report not found")
    return {"status": now}


@router.post("/v1/admin/reply-reports/{report_id}/handled")
async def mark_reply_report_handled(
    report_id: int = Path(ge=1, le=_ID_MAX),
    user: UserContext | None = Depends(get_current_user),
):
    """Mark an open report handled: it leaves the open list with its state.
    A closed report keeps its state; the answer is the state it has."""
    return await _set_state(user, report_id, reports.HANDLED)


@router.post("/v1/admin/reply-reports/{report_id}/dismiss")
async def dismiss_reply_report(
    report_id: int = Path(ge=1, le=_ID_MAX),
    user: UserContext | None = Depends(get_current_user),
):
    """Dismiss an open report to the closed list. A closed report keeps its
    state; the answer is the state it has."""
    return await _set_state(user, report_id, reports.DISMISSED)


@router.post("/v1/admin/reply-reports/{report_id}/restore")
async def restore_reply_report(
    report_id: int = Path(ge=1, le=_ID_MAX),
    user: UserContext | None = Depends(get_current_user),
):
    """Bring a closed report back to the open list (idempotent)."""
    return await _set_state(user, report_id, reports.OPEN)


@router.delete("/v1/admin/reply-reports/{report_id}")
async def delete_reply_report(
    report_id: int = Path(ge=1, le=_ID_MAX),
    user: UserContext | None = Depends(get_current_user),
):
    """Delete a report and its copy of the reply. The person may report that
    reply again."""
    u = require_admin(user)
    if not await run_db(reports.delete, report_id):
        raise HTTPException(status_code=404, detail="Report not found")
    logger.info("reply report %s deleted by %s", report_id, u.sub)
    return {"status": "deleted"}
