"""Cards in the chat (api/cards/cards.py is the route): the draw, the state a person reads, and
the click. The server renders every ``do`` button's words from the stored
parameters, claims a click in the database before anything runs, and runs
the action with the clicker's own role; a ``say`` button's message goes out
as the clicker's own through the chat's queue (``say.py``). A card is drawn
only where a person is at the keyboard (``person_present``) and is live for
everyone who may open its chat: it acts for whoever presses it, never as
the person whose turn asked for it, whom the row keeps only as who asked.
It expires when the chat's session moved on, when the chat is gone, or
after ``CARD_TTL_S``. A list card (``lists.py``) is a block alone: no row,
no click.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from core.session import session_kind, session_state, visibility
from services.cards import actions as catalog, lists, say, status as st
from storage import database as task_store
from storage.cards import db_cards

logger = logging.getLogger("otodock.cards")


class CardRefused(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


# ---------------------------------------------------------------------------
# Who is there
# ---------------------------------------------------------------------------

def person_present(session_id: str, chat: dict[str, Any]) -> bool:
    """Whether a person is at the keyboard of the session drawing a card:
    the session's own kind when it was recorded (a dashboard session or a
    chat lane a person delegated is attended; a scheduled run, a trigger, a
    call and a meeting are not), else the chat row's kind. So a task chat a
    person reopened in the dashboard draws, and a scheduled run's token in
    its creator's chat does not."""
    kind = session_kind.of(session_state.get_session_client_type(session_id))
    if kind is not None:
        return kind.attended
    return session_kind.of_chat(chat).attended


def may_open(u, chat: dict[str, Any]) -> bool:
    """Whether a signed-in person reads and presses the cards of ``chat``:
    an admin, or access to its agent and the chat's open rule (which admits
    a chat's owner before any agent check, so the agent's access is asked
    first). Synchronous."""
    if u.is_admin:
        return True
    return u.can_access_agent(str(chat.get("agent") or "")) and visibility.can_access_chat(u, chat)


# ---------------------------------------------------------------------------
# The draw
# ---------------------------------------------------------------------------

async def draw(*, chat_id: str, session_id: str, agent: str, person_sub: str,
               buttons: list[dict[str, Any]], note: str, u, title: str = "",
               text: str = "") -> dict[str, Any]:
    """The card row. ``buttons`` as the tool sent them: ``{id, kind,
    action, params}`` for a ``do`` button (its words come from the action),
    ``{id, label, kind, message}`` for a ``say`` button. ``title`` and
    ``text`` are the agent's own words: a card with ``say`` buttons alone
    wears them, a card with a ``do`` button wears the action's and keeps the
    agent's text as the note when the note is empty. The person is always
    required (the route holds the gate); each action's draw-time check
    refuses with its sentence; the parameters are validated against the
    action's schema and stored as they are, never again from a client."""
    if not isinstance(buttons, list) or not buttons:
        raise CardRefused(400, "A card needs one to three buttons.")
    if len(buttons) > db_cards.MAX_BUTTONS:
        raise CardRefused(400, f"A card holds at most {db_cards.MAX_BUTTONS} buttons.")
    if len(note or "") > db_cards.MAX_NOTE:
        raise CardRefused(400, f"The note is longer than {db_cards.MAX_NOTE} characters.")
    own_title = " ".join(str(title or "").split())
    own_text = str(text or "").strip()
    if len(own_title) > db_cards.MAX_TITLE:
        raise CardRefused(400, st.TITLE_TOO_LONG)
    if len(own_text) > db_cards.MAX_TEXT:
        raise CardRefused(400, st.TEXT_TOO_LONG)
    if not person_sub:
        raise CardRefused(403, st.NO_PERSON)
    # One action per card: its title, its text and its options are the
    # card's, and the options are passed to the button pressed.
    if sum(1 for b in buttons if isinstance(b, dict) and b.get("kind") == st.BUTTON_DO) > 1:
        raise CardRefused(400, st.ONE_DO_BUTTON)
    stored: list[dict[str, Any]] = []
    options: dict[str, dict[str, Any]] = {}
    title = ""
    text_parts: list[str] = []
    ids: set[str] = set()
    for raw in buttons:
        if not isinstance(raw, dict):
            raise CardRefused(400, "A button must be an object.")
        bid = str(raw.get("id") or "")
        kind = str(raw.get("kind") or "")
        if not bid or len(bid) > 40 or bid in ids:
            raise CardRefused(400, "Every button needs its own short id.")
        ids.add(bid)
        if kind == st.BUTTON_SAY:
            message = str(raw.get("message") or "").strip()
            label = str(raw.get("label") or "").strip()
            if not message or len(message) > db_cards.MAX_TEXT or not label or len(label) > db_cards.MAX_LABEL:
                raise CardRefused(400, "A say button needs a label and a message.")
            stored.append({"id": bid, "label": label, "kind": kind, "message": message})
            continue
        if kind != st.BUTTON_DO:
            raise CardRefused(400, f"A button's kind is one of {', '.join(st.BUTTON_KINDS)}.")
        action = catalog.get(str(raw.get("action") or ""))
        if action is None:
            raise CardRefused(400, f"There is no action named {raw.get('action')!r}.")
        params: dict[str, Any] = dict(raw["params"]) if isinstance(raw.get("params"), dict) else {}
        if "_agent" in action.schema.get("properties", {}):
            params["_agent"] = agent
        problems = catalog.validate_params(action, params)
        if problems:
            raise CardRefused(400, catalog.sentence("The parameters do not fit the action: " + "; ".join(problems)))
        if len(json.dumps(params).encode("utf-8")) > db_cards.MAX_PARAMS_BYTES:
            raise CardRefused(400, f"The parameters are larger than {db_cards.MAX_PARAMS_BYTES // 1024} KB.")
        refusal = await action.check(params, u)
        if refusal:
            raise CardRefused(409, catalog.sentence(refusal))
        words = await action.words(params, u)
        title = title or words["title"]
        if words.get("text"):
            text_parts.append(words["text"])
        for opt in action.options(params, u):
            options.setdefault(str(opt["id"]), {"id": str(opt["id"]), "label": str(opt["label"])[:db_cards.MAX_LABEL * 2],
                                                 "default": bool(opt.get("default"))})
        stored.append({"id": bid, "label": words["label"][:db_cards.MAX_LABEL], "kind": kind,
                       "action": action.name, "params": params})
    note = (note or "").strip()
    if title:
        # The action's words won; the agent's text is its own note.
        if own_text and not note:
            note = own_text[:db_cards.MAX_NOTE]
    else:
        title = own_title or "A question from the agent"
        text_parts = [own_text] if own_text else []
    since = _since()
    if await asyncio.to_thread(db_cards.open_cards_in_chat, chat_id, session_id, since) >= db_cards.MAX_OPEN_PER_CHAT:
        raise CardRefused(409, TOO_MANY_OPEN)
    return await asyncio.to_thread(
        db_cards.create_card,
        chat_id=chat_id, session_id=session_id, agent=agent, person_sub=person_sub, title=title[:db_cards.MAX_TITLE],
        text=" ".join(text_parts)[:db_cards.MAX_TEXT], note=note, buttons=stored,
        options=list(options.values())[:db_cards.MAX_OPTIONS])


TOO_MANY_OPEN = "This chat has too many open cards. Answer or ignore some first."


def _since() -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=st.CARD_TTL_S)).isoformat()


async def draw_list(*, chat_id: str, agent: str, person_sub: str, kind: str, filters: Any, u) -> dict[str, Any]:
    """A list card's block: the kind validated with its filters, the
    person's role against the kind's, the open-card cap over the chat's
    list cards. No row: the dashboard fetches the list for each viewer.
    ``person`` is who asked, never who may read it."""
    if not person_sub:
        raise CardRefused(403, st.NO_PERSON)
    kind_row, clean, problems = lists.validate(kind, filters)
    if problems:
        raise CardRefused(404 if problems[0] == st.NO_SUCH_LIST else 400, problems[0])
    refusal = lists.check(kind_row, agent, u)
    if refusal:
        raise CardRefused(409, refusal)
    if await asyncio.to_thread(db_cards.list_cards_in_chat, chat_id, _since()) >= db_cards.MAX_OPEN_PER_CHAT:
        raise CardRefused(409, TOO_MANY_OPEN)
    return {"kind": kind_row.name, "filters": clean, "title": lists.title_of(kind_row, clean), "person": person_sub}


def _button_of(b: dict[str, Any]) -> dict[str, Any]:
    out = {"id": b["id"], "label": b["label"], "kind": b["kind"]}
    if b["kind"] == st.BUTTON_SAY:
        # The person's own words to be: shown on the button, never a parameter.
        out["message"] = b.get("message") or ""
    return out


def block_of(card: dict[str, Any]) -> dict[str, Any]:
    """What the chat block and the frame carry: the words and the buttons'
    ids, labels and kinds (a say button's message too), never the
    parameters."""
    return {
        "card_id": card["id"], "title": card["title"], "text": card.get("text") or "",
        "note": card.get("note") or "",
        "buttons": [_button_of(b) for b in card["buttons"]],
        "options": [{"id": o["id"], "label": o["label"], "default": bool(o.get("default"))}
                    for o in card.get("options") or []],
    }


# ---------------------------------------------------------------------------
# The state
# ---------------------------------------------------------------------------

def expiry(card: dict[str, Any], chat: dict[str, Any] | None = None) -> str | None:
    """Why the card no longer takes a click, or None while it does."""
    if chat is None:
        chat = task_store.get_chat(card["chat_id"])
    if chat is None:
        return st.CHAT_GONE
    if (chat.get("session_id") or "") != card["session_id"]:
        return st.EXPIRED
    try:
        created = datetime.fromisoformat(card["created_at"])
    except ValueError:
        return st.EXPIRED
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - created).total_seconds() > st.CARD_TTL_S:
        return st.EXPIRED
    return None


def _display_name(sub: str) -> str:
    """A person's name as another person reads it, or "" when unknown."""
    row = (task_store.get_user(sub) if sub else None) or {}
    return str(row.get("display_name") or row.get("name") or row.get("username") or "")


def _name_of(sub: str, names: dict[str, str]) -> str:
    if sub not in names:
        names[sub] = _display_name(sub) or "Someone"
    return names[sub]


def state(card: dict[str, Any], viewer_sub: str | None = None) -> dict[str, Any]:
    """The card as ``viewer_sub`` reads it: the words, the buttons, the
    clicks so far (who pressed, and whether it was the reader), the outcome
    (a running job's live progress folded in) and whether it expired."""
    outcome = card.get("outcome")
    if isinstance(outcome, dict) and outcome.get("status") == st.OUTCOME_RUNNING and outcome.get("job_id"):
        action = _action_of_click(card)
        if action is not None and action.progress is not None:
            outcome = action.progress(catalog.Outcome(outcome["status"], str(outcome.get("text") or ""),
                                                      str(outcome.get("job_id") or "")))
            if outcome.get("status") != st.OUTCOME_RUNNING:
                db_cards.set_outcome(card["id"], outcome)
    names: dict[str, str] = {}
    return {
        **block_of(card),
        "chat_id": card["chat_id"], "agent": card["agent"], "created_at": card["created_at"],
        "resolved": bool(card.get("resolved_at")),
        "clicks": [{"button": c["button"], "status": c["status"], "at": c["clicked_at"], "detail": c["detail"],
                    "by": _name_of(c["person_sub"], names),
                    "mine": bool(viewer_sub) and c["person_sub"] == viewer_sub}
                   for c in db_cards.clicks_of(card["id"])],
        "outcome": outcome,
        # An answered card keeps reading as answered: it no longer takes a
        # click, whatever happened to the chat's session since.
        "expired": None if card.get("resolved_at") else expiry(card),
    }


async def _record_interrupted(card_id: str, click_id: str) -> None:
    failed = catalog.Outcome(st.OUTCOME_FAILED, st.INTERRUPTED)
    await asyncio.to_thread(db_cards.finish_click, click_id, db_cards.CLICK_FAILED, failed.text)
    await asyncio.to_thread(db_cards.set_outcome, card_id, failed.as_dict())


def _action_of_click(card: dict[str, Any]) -> catalog.Action | None:
    for c in db_cards.clicks_of(card["id"]):
        button = next((b for b in card["buttons"] if b["id"] == c["button"]), None)
        if button and button.get("kind") == st.BUTTON_DO:
            return catalog.get(str(button.get("action") or ""))
    return None


# ---------------------------------------------------------------------------
# The click
# ---------------------------------------------------------------------------

async def act(card: dict[str, Any], button_id: str, options: dict[str, Any] | None, u) -> dict[str, Any]:
    """One click, and one per card, by anyone who may open the chat: a
    ``say`` button sends its message as the presser's own through the
    chat's queue, a ``do`` button runs its action with ``u`` and records the
    outcome; either claims the card first. A chat held on another person's
    own computer is theirs alone while it is. A presser other than the
    person who asked meets the action's own check first, so a press their
    role could never complete spends no click, and is refused a card about
    the asker's own files, memory or personal apps. Refusals carry the
    sentence the person reads."""
    from services.remote import chat_move
    chat = await asyncio.to_thread(task_store.get_chat, card["chat_id"])
    if chat is None:
        raise CardRefused(409, st.CHAT_GONE)
    if not await asyncio.to_thread(may_open, u, chat):
        raise CardRefused(403, st.CHAT_NOT_OPEN)
    presser = u.acting_sub or ""
    lock = await asyncio.to_thread(chat_move.lock_sentence, chat, presser)
    if lock:
        raise CardRefused(403, lock)
    gone = expiry(card, chat)
    if gone:
        raise CardRefused(409, gone)
    button = next((b for b in card["buttons"] if b["id"] == button_id), None)
    if button is None:
        raise CardRefused(400, st.NO_SUCH_BUTTON)
    if card.get("resolved_at"):
        raise CardRefused(409, st.ANSWERED_ALREADY)
    chosen = {o["id"]: bool((options or {}).get(o["id"], o.get("default")))
              for o in card.get("options") or []}
    asker = bool(card["person_sub"]) and presser == card["person_sub"]
    if button["kind"] == st.BUTTON_SAY:
        try:
            await say.check(chat, u)
        except say.SayRefused as exc:
            raise CardRefused(exc.status, exc.detail) from exc
        agent = str(chat.get("agent") or "")
        if not asker and await asyncio.to_thread(chat_move.placement_blocked, chat, presser,
                                                 u.effective_role(agent)):
            raise CardRefused(403, st.PLACE_NOT_YOURS)
    elif not asker:
        pre = catalog.get(str(button.get("action") or ""))
        params = dict(button.get("params") or {})
        what = pre.personal(params) if pre is not None else ""
        if what:
            name = await asyncio.to_thread(_display_name, card["person_sub"]) or "another person"
            raise CardRefused(403, st.ASKERS_OWN.format(name=name, what=what))
        refusal = await pre.check(params, u) if pre is not None else None
        if refusal:
            raise CardRefused(403, catalog.sentence(refusal))
    click = await asyncio.to_thread(db_cards.claim_click, card["id"], button_id, presser, chosen, resolves=True)
    if click is None:
        raise CardRefused(409, st.ANSWERED_ALREADY)
    if button["kind"] == st.BUTTON_SAY:
        try:
            sent = await say.send(chat, card["id"], button_id, button["message"], u)
        except Exception as exc:  # the click is given back, the log has the trace
            logger.exception("card %s: the say message was not queued", card["id"])
            await asyncio.to_thread(db_cards.unclaim, click, resolves=True)
            raise CardRefused(503, st.NO_ANSWER) from exc
        if not sent:
            await asyncio.to_thread(db_cards.unclaim, click, resolves=True)
            raise CardRefused(409, st.TOO_MANY_QUEUED)
        await asyncio.to_thread(db_cards.finish_click, click["id"], db_cards.CLICK_SAID, button["message"])
        fresh = await asyncio.to_thread(db_cards.get_card, card["id"]) or card
        return {"kind": st.BUTTON_SAY, "queued": True, "state": await asyncio.to_thread(state, fresh, presser)}
    action = catalog.get(str(button.get("action") or ""))
    if action is None:
        outcome = catalog.Outcome(st.OUTCOME_FAILED, "This action is not in this version.")
    else:
        try:
            outcome = await action.run(dict(button.get("params") or {}), chosen, u)
        except Exception as exc:  # the click's row carries the failure, the log the trace
            logger.exception("card %s: action %s failed", card["id"], action.name)
            outcome = catalog.Outcome(st.OUTCOME_FAILED, catalog._refusal_text(exc))
        except BaseException:
            # Interrupted mid-action (the request cancelled): the row is
            # finished as failed, shielded, so it never stays running.
            await asyncio.shield(_record_interrupted(card["id"], click["id"]))
            raise
    outcome = catalog.Outcome(outcome.status, catalog.sentence(outcome.text), outcome.job_id, outcome.extra)
    await asyncio.to_thread(
        db_cards.finish_click, click["id"],
        db_cards.CLICK_DONE if outcome.status != st.OUTCOME_FAILED else db_cards.CLICK_FAILED, outcome.text)
    await asyncio.to_thread(db_cards.set_outcome, card["id"], outcome.as_dict())
    fresh = await asyncio.to_thread(db_cards.get_card, card["id"]) or card
    return {"kind": st.BUTTON_DO, "outcome": outcome.as_dict(), "state": await asyncio.to_thread(state, fresh, presser)}
