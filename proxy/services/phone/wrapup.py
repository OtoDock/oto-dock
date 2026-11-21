"""The wrap-up after a conversation: a new run of the same agent, never a
reload of the call's session, that records what happened.

When a call of the agent's Conversations tab ends (``ws/phone.py``'s
teardown), :func:`after_call` claims the conversation's record once and
:func:`_begin` decides, the one place every wrap-up passes: with an external
identity and the agent's switch off (``outside_store.get_wrapup``), nothing
runs; when the person said nothing it writes the outcome "No answer." itself
and runs nothing. Otherwise it starts a task run of the kind ``wrapup``
(``task_kinds.WRAPUP``, in memory like a check's judge) whose config is the
phone builder's (``phone_config_builder.build_wrapup_agent_config``: the
business tools, no shell, no web, the person's memory and files alone), with
the transcript fenced as data in its prompt. The run records the outcome and
the follow-ups through the conversations tool; what it wrote and how it
ended go on the record, and one event row (``wire.WRAPUP``) shows them under
the conversation.

A wrap-up runs once at most (:func:`resume_after_boot`): one a restart cut
is closed, since it may have used the agent's tools already, and one that
never started is started. After a restore or a long downtime none starts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import config
from adapters.phone import is_outbound
from core import placement
from core.background import spawn
from core.events.common_events import ROW_ASSISTANT, ROW_USER
from core.session import external_identity
from services.infra.external_data import NOTE, fence
from storage import database as task_store
from storage.chat import conversation_store
from storage.pg import run_db

logger = logging.getLogger("otodock.wrapup")

#: The task run's name and its chat's title.
TITLE = "After the call"
#: How long a wrap-up may run, and how many times a record that never
#: started is tried.
RUN_TIMEOUT_S = 900
MAX_ATTEMPTS = 2
DID_NOT_FINISH = "The wrap-up did not finish."
#: The transcript's caps: one row, the whole (the oldest rows go first).
ROW_MAX_CHARS = 2_500
TRANSCRIPT_MAX_CHARS = 24_000

# The phone service's own prompts into a call (phone/pipeline/outbound.py):
# the opening of an outbound call, the hold while it asks the agent that
# placed it, and the summary it asks for at the end. None is the person
# speaking.
_DAEMON_OPENING = "The person just answered the phone."
_DAEMON_PREFIXES = ("[HOLD: You asked a question", "The call has ended. Based on the conversation")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def is_daemon_prompt(text: str, *, outbound: bool) -> bool:
    """Whether a user row of a call is the phone service's own prompt. It
    sends them into an outbound call alone: on an inbound one every word is
    the caller's, whatever it starts with."""
    if not outbound:
        return False
    text = (text or "").lstrip()
    if text.startswith("Task: ") and _DAEMON_OPENING in text:
        return True
    return text.startswith(_DAEMON_PREFIXES)


def person_spoke(rows: list[dict], *, outbound: bool) -> bool:
    """Whether the person on the line said anything: a user row that is not
    one of the phone service's own prompts."""
    return any(
        r.get("role") == ROW_USER and (r.get("content") or "").strip()
        and not is_daemon_prompt(r.get("content") or "", outbound=outbound)
        for r in rows
    )


def render_transcript(rows: list[dict], *, outbound: bool) -> str:
    """The conversation, one fenced line per row, oldest first, cut from the
    oldest end to :data:`TRANSCRIPT_MAX_CHARS`. Tool rows are left out."""
    lines: list[str] = []
    for r in rows:
        role, text = r.get("role"), (r.get("content") or "").strip()
        if not text or role not in (ROW_USER, ROW_ASSISTANT):
            continue
        who = (("Line" if is_daemon_prompt(text, outbound=outbound) else "Person")
               if role == ROW_USER else "The agent")
        lines.append(f"{who}: {fence(text[:ROW_MAX_CHARS])}")
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if total + len(line) + 1 > TRANSCRIPT_MAX_CHARS:
            break
        kept.append(line)
        total += len(line) + 1
    kept.reverse()
    cut = len(kept) < len(lines)
    head = ("The conversation, oldest first. Person is the outside person, Agent is you, "
            "Line is the phone service's own prompt."
            + (" Its oldest lines were cut." if cut else ""))
    return f"{head}\n{NOTE}\n\n" + "\n".join(kept)


def _home_of(record: dict) -> str:
    """The tree of the outside person the conversation was with ("" when it
    has none: a withheld number, a Shared-only agent's caller, the agent's
    own line), from their record."""
    if not record.get("identity_id"):
        return ""
    from storage.identity import external_identity_store
    row = external_identity_store.get_by_id(record["identity_id"])
    if row is None or row.get("agent") != record.get("agent"):
        return ""
    try:
        home = external_identity.tree_of(config.get_agent_dir(record["agent"]),
                                         row["channel"], row["slug"])
    except ValueError:
        return ""
    return str(home) if home.is_dir() else ""


async def after_call(chat_id: str) -> None:
    """The call of ``chat_id`` ended: claim its record's wrap-up and run it,
    or write "No answer." when the person said nothing. Nothing for a call
    with no record (a person's own call) or one already wrapping up."""
    record = await run_db(conversation_store.claim_wrapup, chat_id)
    if record is None:
        return
    rows = await run_db(task_store.get_chat_messages, chat_id)
    await _begin(record, rows)


def _switched_off(record: dict) -> bool:
    """Whether the agent's managers turned off the wrap-up after a
    conversation with an external identity (the agent's own line keeps it).
    Synchronous."""
    from storage.agents import outside_store
    return (record.get("line") in conversation_store.EXTERNAL_LINES
            and not outside_store.get_wrapup(record["agent"])["wrapup_enabled"])


async def _begin(record: dict, rows: list[dict]) -> bool:
    """The one place a claimed record's wrap-up is decided, after a call and
    at the boot: off for an external identity, it ends there and nothing
    runs (no card: nothing happened); with no words from the person, "No
    answer." and its card; else the run starts. Returns whether it did."""
    chat_id = record["chat_id"]
    if await run_db(_switched_off, record):
        await run_db(conversation_store.set_wrapup, chat_id, conversation_store.WRAPUP_OFF)
        return False
    if not person_spoke(rows, outbound=is_outbound(record.get("direction"))):
        await run_db(conversation_store.set_wrapup, chat_id, conversation_store.WRAPUP_SKIPPED,
                     outcome=conversation_store.NO_ANSWER)
        await _post_card(chat_id)
        return False
    spawn(_run(record, rows), name=chat_id[:8], owner="wrapup")
    return True


async def resume_after_boot() -> int:
    """The wrap-ups a restart or a stop left open. One whose run started is
    closed: it may have used the agent's tools already. One that never
    started is started while it has attempts left. After a restore or a long
    downtime (``platform_marks.boot().catches_up`` false) every open one is
    closed and none starts. Returns how many were started."""
    from core.session.session_state import session_is_live
    from services.infra import platform_marks
    catches_up = platform_marks.boot().catches_up
    started = 0
    for record in await run_db(conversation_store.unfinished):
        run = await run_db(task_store.get_run, record.get("wrapup_run_id") or "") \
            if record.get("wrapup_run_id") else None
        if run and session_is_live(run.get("session_id") or ""):
            continue
        chat_id = record["chat_id"]
        if (not catches_up or record.get("wrapup_run_id")
                or await run_db(_run_began, chat_id)
                or int(record.get("wrapup_attempts") or 0) >= MAX_ATTEMPTS):
            await run_db(conversation_store.set_wrapup, chat_id, conversation_store.WRAPUP_FAILED,
                         outcome=record.get("outcome") or DID_NOT_FINISH)
            await _post_card(chat_id)
            continue
        await run_db(conversation_store.set_wrapup, chat_id, "")
        rows = await run_db(task_store.get_chat_messages, chat_id)
        claimed = await run_db(conversation_store.claim_wrapup, chat_id)
        if claimed is not None and await _begin(claimed, rows):
            started += 1
    if started:
        logger.info("wrap-up: %d left open by the restart started again", started)
    return started


def _run_began(chat_id: str) -> bool:
    """Whether a run of the conversation's wrap-up exists: a restart between
    its start and the record naming it leaves the record with no run id."""
    return bool(task_store.list_runs(limit=1, task_id=conversation_store.wrapup_task_id(chat_id)))


async def _run(record: dict, rows: list[dict]) -> None:
    chat_id = record["chat_id"]
    t0 = time.time()
    home = await run_db(_home_of, record)
    try:
        run_id = await _start(record, rows, home)
        if run_id is None:
            await run_db(conversation_store.set_wrapup, chat_id, conversation_store.WRAPUP_OFF)
            return
    except Exception as e:  # noqa: BLE001  # the record says why; the call's teardown is done
        logger.warning("wrap-up of %s did not start: %s", chat_id[:8], e)
        await run_db(conversation_store.set_wrapup, chat_id, conversation_store.WRAPUP_FAILED,
                     outcome="The wrap-up could not start.")
        await _post_card(chat_id)
        return
    await run_db(conversation_store.set_wrapup, chat_id, conversation_store.WRAPUP_RUNNING,
                 run_id=run_id)
    from services.scheduler import run_wait
    from storage.automation import run_status
    try:
        status = await run_wait.wait_run(run_id, timeout=RUN_TIMEOUT_S, cancelled=lambda: False,
                                         what="the wrap-up")
        run = await run_db(task_store.get_run, run_id) or {}
        await run_db(_note_files_written, chat_id, home, t0)
        await asyncio.to_thread(_prune_scratch, record["agent"], run.get("session_id") or "")
        current = await run_db(conversation_store.get, chat_id) or {}
        outcome = current.get("outcome") or ""
        if status == run_status.COMPLETED:
            state = conversation_store.WRAPUP_DONE
            outcome = outcome or await run_db(_last_text, session_chat_of(run_id))
        else:
            # A run cut off mid-sentence has no outcome to quote.
            state, outcome = conversation_store.WRAPUP_FAILED, outcome or DID_NOT_FINISH
    except Exception as e:  # noqa: BLE001  # an open record keeps the person unforgettable
        logger.warning("wrap-up of %s ended in an error: %s", chat_id[:8], e)
        current = await run_db(conversation_store.get, chat_id) or {}
        state, outcome = conversation_store.WRAPUP_FAILED, current.get("outcome") or DID_NOT_FINISH
    await run_db(conversation_store.set_wrapup, chat_id, state, outcome=outcome)
    await _post_card(chat_id)


async def _start(record: dict, rows: list[dict], home: str) -> str | None:
    """The run, started; None when the switch went off since :func:`_begin`
    (read here, right before the start, so it never starts when off)."""
    from services.scheduler import scheduler, task_kinds
    from services.scheduler.shared import TaskDefinition
    from storage.agents import outside_store
    agent, chat_id = record["agent"], record["chat_id"]
    settings = await run_db(outside_store.get_wrapup, agent)
    if record.get("line") in conversation_store.EXTERNAL_LINES and not settings["wrapup_enabled"]:
        return None
    prompt = render_transcript(rows, outbound=is_outbound(record.get("direction")))
    task = TaskDefinition(
        id=conversation_store.wrapup_task_id(chat_id), name=TITLE, agent=agent, prompt=prompt,
        timeout_seconds=RUN_TIMEOUT_S, created_by=None, scope="agent",
        notification_mode="none", task_type=task_kinds.WRAPUP,
        override_model=config.live_model_id(settings.get("wrapup_model") or "") or None,
        execution_target_override=placement.LOCAL,
        wrapup={"chat_id": chat_id, "identity_id": record.get("identity_id") or "",
                "home": home},
    )
    return await scheduler.trigger_task_now(task, trigger_type=task_kinds.TRIGGER_MANUAL,
                                            trigger_source=f"wrapup:{chat_id}")


def session_chat_of(run_id: str) -> str:
    """The chat a task run writes its turns into."""
    from core.session import session_kind
    return session_kind.task_chat_id(run_id)


def _last_text(chat_id: str) -> str:
    """The run's last answer, the outcome when the record tool was not
    called (one line, at most 300 characters)."""
    for row in reversed(task_store.get_chat_messages(chat_id)):
        if row.get("role") == ROW_ASSISTANT and (row.get("content") or "").strip():
            return " ".join(row["content"].split())[:300]
    return ""


def _note_files_written(chat_id: str, home: str, since: float) -> None:
    """The files the run wrote into the person's tree, on the record."""
    if not home:
        return
    from services.phone.people import person_files
    for path, rel in person_files(Path(home), memory=False):
        if path.stat().st_mtime >= since:
            conversation_store.add_wrote(chat_id, {"kind": "file", "name": rel})


def _prune_scratch(agent: str, session_id: str) -> None:
    """The run's scratch home (its engine state), gone with the run."""
    external_identity.prune_session_scratch(config.get_agent_dir(agent), session_id)


async def _post_card(chat_id: str) -> None:
    """One event row under the conversation: how the wrap-up ended, the
    outcome, the follow-ups and what it wrote."""
    from ws import wire_events as wire
    record = await run_db(conversation_store.get, chat_id)
    if record is None:
        return
    data = {
        "state": record.get("wrapup") or "",
        "outcome": record.get("outcome") or "",
        "follow_ups": record.get("follow_ups") or [],
        "wrote": record.get("wrote") or [],
        "run_id": record.get("wrapup_run_id") or "",
    }
    await run_db(task_store.add_chat_message, chat_id, "event", "",
                 event_type=wire.WRAPUP, event_data=json.dumps(data))


def record_from_session(session_id: str, outcome: str, follow_ups: list[str], note: str) -> str:
    """What the wrap-up's record tool sends (``POST /v1/conversations/record``):
    the outcome and the follow-ups on the record of the conversation the
    session wraps up, and the note for the team when the agent keeps them.
    Returns "" or the sentence why nothing was recorded. Synchronous."""
    from core.session.session_state import get_session_security
    from storage.agents import outside_store
    ctx = get_session_security(session_id)
    chat_id = getattr(ctx, "wrapup", "") or ""
    record = conversation_store.get(chat_id) if chat_id else None
    if record is None:
        return "This session wraps up no conversation."
    outcome = " ".join((outcome or "").split())[:500]
    if not outcome:
        return "The outcome is one sentence, and it is empty."
    conversation_store.record_outcome(chat_id, outcome, [
        " ".join(str(f).split())[:300] for f in (follow_ups or []) if str(f).strip()][:20])
    note = " ".join(_CONTROL_CHARS.sub(" ", note or "").split())[:conversation_store.NOTE_MAX_CHARS]
    if note and outside_store.get_wrapup(record["agent"])["shared_notes"]:
        conversation_store.add_note(record["agent"], chat_id, record.get("identity_id") or "", note)
    return ""
