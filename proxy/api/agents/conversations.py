"""The agent's Conversations tab: the conversations of outside people and of
the agent's own line, and the people who reached the agent. Open to the
agent's editors and managers (a 1.9.1 call on a route tied to a person stays
the managers'), never to a session token or a key. Attaches to the shared
package router."""

import asyncio
import logging
import os

from fastapi import Depends, HTTPException
from pydantic import BaseModel

import config
from api.agents._router import router
from auth import roles, tokens
from auth.providers import UserContext, get_current_user, require_auth, require_edit
from core.session.visibility import PHONE_CHAT_OWNER
from services.infra.external_data import NOTE
from storage.chat import conversation_store
from storage.identity import external_identity_store

logger = logging.getLogger("otodock")


def _reader(user: UserContext | None, agent: str) -> tuple[UserContext, tuple[str, ...]]:
    """The person reading the tab and the lines they read: an editor or above
    on a person's dashboard; the ``person`` line a manager's alone."""
    u = require_auth(user)
    # A person's tab: an agent-scope session (a task run, a trigger, a call)
    # or a key reads no caller's transcript, whatever role it carries.
    if u.is_no_user_session or u.is_api_key:
        raise HTTPException(status_code=403, detail="The conversations are for people")
    require_edit(u, agent)
    lines = (conversation_store.LINES if u.can_manage_agent(agent)
             else conversation_store.EDITOR_LINES)
    return u, lines


@router.get("/v1/agents/{name}/conversations")
async def list_agent_conversations(
    name: str,
    person: str = "",
    offset: int = 0,
    limit: int = 50,
    user: UserContext = Depends(get_current_user),
):
    """The agent's conversations, the most recent first, each with its record
    (the line, the person, the outcome, the wrap-up). ``person`` narrows to
    one outside person's."""
    _u, lines = _reader(user, name)
    limit = max(1, min(limit, config.MAX_PAGE_SIZE))  # cap page size
    conversations = await asyncio.to_thread(
        conversation_store.list_for_agent, name, owner=PHONE_CHAT_OWNER, lines=lines,
        person=person, offset=offset, limit=limit,
    )
    total = await asyncio.to_thread(
        conversation_store.count_for_agent, name, owner=PHONE_CHAT_OWNER, lines=lines,
        person=person,
    )
    return {"conversations": conversations, "total": total}


# --- the people view -----------------------------------------------------------

@router.get("/v1/agents/{name}/people")
async def list_agent_people(name: str, user: UserContext = Depends(get_current_user)):
    """The outside people who reached the agent, the most recent first."""
    _reader(user, name)
    from services.phone import people
    return {"people": await asyncio.to_thread(people.people_of, name)}


@router.get("/v1/agents/{name}/people/{identity_id}")
async def get_agent_person(name: str, identity_id: str, user: UserContext = Depends(get_current_user)):
    """One outside person: their memory, their files, their conversations."""
    _reader(user, name)
    from services.phone import people
    person = await asyncio.to_thread(people.person_of, name, identity_id)
    if person is None:
        raise HTTPException(404, "This person is not on record for this agent.")
    return person


#: What one person's export may hold, at most, and what a larger one answers.
EXPORT_MAX_MB = 200
EXPORT_TOO_BIG = f"This person's files come to more than {EXPORT_MAX_MB} MB, so the export was not made."


def _export_zip(name: str, identity_id: str) -> tuple[int, os.stat_result, str] | None:
    """One person's export: their record, memory and files, conversations,
    notes and call-log rows, and nobody else's, written to an unnamed temp file
    as the workspace zip is (``api/agents/files``: by descriptor, no link
    followed). Returns ``(fd, stat, file name)``; the caller owns the fd. None
    when they are not the agent's. Past :data:`EXPORT_MAX_MB` it answers 413."""
    import json
    import tempfile
    import zipfile

    from api.agents.files import _zip_add_regular_file, _zip_tmp_dir, _ZipBudget
    from services.infra import safe_fs
    from services.phone import people
    from storage.phone import phone_call_log_store
    row = external_identity_store.get_by_id(identity_id)
    if row is None or row.get("agent") != name:
        return None
    budget = _ZipBudget()
    budget.max_bytes = EXPORT_MAX_MB * 1024 * 1024
    tmp = tempfile.TemporaryFile(dir=_zip_tmp_dir())
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("person.json", json.dumps({
                k: row.get(k) for k in ("channel", "external_id", "first_seen", "last_seen",
                                        "verified_at", "conversations")}, indent=2))
            home = people.tree_of_row(row)
            if home is not None:
                with safe_fs.open_root(config.AGENTS_DIR, name) as agent_fd:
                    base = home.relative_to(config.get_agent_dir(name)).as_posix()
                    for _path, rel in people.export_files_of(row):
                        _zip_add_regular_file(zf, agent_fd, f"{base}/{rel}", f"files/{rel}", budget)
            for record in conversation_store.for_identity(name, identity_id):
                stamp = (record.get("started_at") or "")[:19].replace(":", "-")
                zf.writestr(f"conversations/{stamp}-{record['chat_id']}.md",
                            people.conversation_markdown(record["chat_id"], record))
            notes = conversation_store.notes_of_identity(name, identity_id)
            if notes:
                zf.writestr("notes.txt", "\n".join(f"{n['day']}: {n['text']}" for n in notes) + "\n")
            calls = phone_call_log_store.rows_for_caller(name, row.get("external_id") or "")
            if calls:
                zf.writestr("calls.json", json.dumps(calls, indent=2, default=str))
        tmp.flush()
        tmp.seek(0)
        fd = os.dup(tmp.fileno())
    except HTTPException as exc:
        if exc.status_code != 413:
            raise
        # The workspace zip's 413 names a selection of folders, which an export has not.
        raise HTTPException(413, EXPORT_TOO_BIG) from exc
    finally:
        tmp.close()
    return fd, os.fstat(fd), f"{row['channel']}-{row['slug']}.zip"


@router.post("/v1/agents/{name}/people/{identity_id}/export-url")
async def request_person_export_url(name: str, identity_id: str, user: UserContext = Depends(get_current_user)):
    """A short-lived signed address of a person's export: the Android app's
    download manager takes an address, never a file the page fetched."""
    u, _lines = _reader(user, name)
    row = await asyncio.to_thread(external_identity_store.get_by_id, identity_id)
    if row is None or row.get("agent") != name:
        raise HTTPException(404, "This person is not on record for this agent.")
    token = tokens.mint(tokens.PERSON_EXPORT, {
        "purpose": "person_export", "agent": name, "person": identity_id, "user_sub": u.sub,
    }, ttl_s=120)
    return {"download_url": f"/v1/agents/{name}/people/{identity_id}/export?t={token}",
            "filename": f"{row['channel']}-{row['slug']}.zip"}


@router.get("/v1/agents/{name}/people/{identity_id}/export")
async def export_agent_person(name: str, identity_id: str, t: str):
    """A zip of what the agent holds about one outside person. The signature
    names the person and the reader, whose role is read again here: a demotion
    since the click ends the export."""
    try:
        claims = tokens.decode(t, tokens.PERSON_EXPORT)
    except tokens.TokenExpired as exc:
        raise HTTPException(410, "Download link expired") from exc
    except tokens.TokenInvalid as exc:
        raise HTTPException(403, "Invalid download token") from exc
    if claims.get("agent") != name or claims.get("person") != identity_id:
        raise HTTPException(403, "Token / person mismatch")
    from api.media.media import FdFileResponse
    from auth.providers import user_context_for_sub
    u, _lines = _reader(await asyncio.to_thread(user_context_for_sub, claims.get("user_sub") or ""), name)
    built = await asyncio.to_thread(_export_zip, name, identity_id)
    if built is None:
        raise HTTPException(404, "This person is not on record for this agent.")
    fd, st, filename = built
    logger.info("people: %s exported one person of %s (%d bytes)", u.sub[:12], name, st.st_size)
    try:
        return FdFileResponse(fd, st, media_type="application/zip", filename=filename)
    except BaseException:
        os.close(fd)
        raise


@router.delete("/v1/agents/{name}/people/{identity_id}")
async def forget_agent_person(name: str, identity_id: str, user: UserContext = Depends(get_current_user)):
    """Forget one outside person: their conversations, memory, files, notes
    and call-log rows, gone from every store (``external_retention.forget_person``).
    Refused while a conversation with them runs."""
    u, _lines = _reader(user, name)
    from services.infra import external_retention, retention
    async with retention._sweep_lock:
        live = retention._build_live_snapshot()
        stats = await asyncio.to_thread(
            external_retention.forget_person, name, identity_id,
            live.busy_external_homes, live.session_ids, live.pump_chat_ids,
        )
    if stats is None:
        raise HTTPException(404, "This person is not on record for this agent.")
    if stats.get("refused"):
        raise HTTPException(409, stats["refused"])
    logger.info("people: %s forgot one person of %s: %s", u.sub[:12], name, stats)
    return stats


# --- the conversations tool and the wrap-up's record (a session's token) -------

def _session_of(user: UserContext | None) -> UserContext:
    u = require_auth(user)
    if not u.is_session or not u.agent:
        raise HTTPException(403, "These routes answer an agent's own session.")
    return u


def _tool_reader(user: UserContext | None) -> tuple[UserContext, tuple[str, ...]]:
    """A person's own session with the editor role or above on its agent,
    and the lines they read."""
    u = _session_of(user)
    if u.acting_sub is None:
        raise HTTPException(403, "The conversations are for an editor's or a manager's own session.")
    role = u.acting_role(u.agent)
    if not roles.can_edit(role):
        raise HTTPException(403, "The conversations are for the agent's editors and managers.")
    return u, (conversation_store.LINES if roles.can_manage(role) else conversation_store.EDITOR_LINES)


@router.get("/v1/conversations/kind")
async def conversations_kind(user: UserContext = Depends(get_current_user)):
    """What this session is to the conversations tool: ``reader`` (a person's
    own session, editor or above), ``wrapup`` (the wrap-up after a
    conversation) or ``""``."""
    u = _session_of(user)
    from core.session.session_state import get_session_security
    if u.acting_sub is None:
        ctx = get_session_security(u.session_id)
        return {"kind": "wrapup" if getattr(ctx, "wrapup", "") else ""}
    return {"kind": "reader" if roles.can_edit(u.acting_role(u.agent)) else ""}


@router.get("/v1/conversations")
async def tool_list_conversations(days: int = 7, person: str = "", limit: int = 50,
                                  user: UserContext = Depends(get_current_user)):
    """The conversations tool's list (``services/phone/people``)."""
    u, lines = _tool_reader(user)
    from services.phone import people
    return {"conversations": await asyncio.to_thread(
        people.conversations_for_tool, u.agent, lines=lines, days=min(max(days, 1), 365),
        person=person, limit=min(max(limit, 1), 200)), "note": NOTE}


@router.get("/v1/conversations/people")
async def tool_list_people(user: UserContext = Depends(get_current_user)):
    """The conversations tool's people."""
    u, _lines = _tool_reader(user)
    from services.phone import people
    return {"people": await asyncio.to_thread(people.people_for_tool, u.agent), "note": NOTE}


@router.get("/v1/conversations/{chat_id}")
async def tool_read_conversation(chat_id: str, user: UserContext = Depends(get_current_user)):
    """One conversation's words for the conversations tool."""
    u, lines = _tool_reader(user)
    from services.phone import people
    got = await asyncio.to_thread(people.conversation_for_tool, u.agent, chat_id, lines=lines)
    if got is None:
        raise HTTPException(404, "There is no such conversation of this agent.")
    return {**got, "note": NOTE}


class RecordRequest(BaseModel):
    outcome: str
    follow_ups: list[str] = []
    note: str = ""


@router.post("/v1/conversations/record")
async def record_conversation(req: RecordRequest, user: UserContext = Depends(get_current_user)):
    """The wrap-up's record tool: the outcome and the follow-ups of the
    conversation the session wraps up, and the note for the team when the
    agent keeps them."""
    u = _session_of(user)
    if u.acting_sub is not None:
        raise HTTPException(403, "Only the wrap-up after a conversation records its outcome.")
    from services.phone import wrapup
    refusal = await asyncio.to_thread(
        wrapup.record_from_session, u.session_id, req.outcome, req.follow_ups, req.note)
    if refusal:
        raise HTTPException(400, refusal)
    return {"ok": True}
