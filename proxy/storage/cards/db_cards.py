"""Cards and their clicks: the rows ``services/cards`` reads and
writes. A click is claimed in the database before anything runs, so two
tabs or a double click never run an action twice.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import psycopg

from storage.pg import get_conn

# A click's states, spelled here alone.
CLICK_RUNNING = "running"
CLICK_DONE = "done"
CLICK_FAILED = "failed"
CLICK_SAID = "said"

# Caps a card holds to (the draw route refuses past them).
MAX_TITLE = 120
MAX_TEXT = 2000
MAX_NOTE = 1000
MAX_LABEL = 40
MAX_BUTTONS = 3
MAX_OPTIONS = 3
MAX_PARAMS_BYTES = 8 * 1024
MAX_OPEN_PER_CHAT = 50
# The row kind a list card's block is stored under (ws.wire_events.LIST_CARD;
# storage never imports the wire catalogue).
LIST_CARD_EVENT = "list_card"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _loads(raw, default):
    if raw is None:
        return default
    return json.loads(raw) if isinstance(raw, str) else raw


def card_row(row: dict | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    out["buttons"] = _loads(out.get("buttons"), [])
    out["options"] = _loads(out.get("options"), [])
    out["outcome"] = _loads(out.get("outcome"), None)
    return out


def click_row(row: dict) -> dict:
    out = dict(row)
    out["options"] = _loads(out.get("options"), {})
    return out


def create_card(*, chat_id: str, session_id: str, agent: str, person_sub: str, title: str,
                text: str, note: str, buttons: list[dict], options: list[dict]) -> dict:
    card_id = f"card-{uuid.uuid4().hex[:12]}"
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_cards (id, chat_id, session_id, agent, person_sub, title, text, note, "
            "buttons, options, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)",
            (card_id, chat_id, session_id, agent, person_sub, title, text, note,
             json.dumps(buttons), json.dumps(options), _now()),
        )
        conn.commit()
    return get_card(card_id)  # type: ignore[return-value]


def get_card(card_id: str) -> dict | None:
    with get_conn() as conn:
        return card_row(conn.execute("SELECT * FROM chat_cards WHERE id = %s", (card_id,)).fetchone())


def open_cards_in_chat(chat_id: str, session_id: str, since: str) -> int:
    """The cards of the chat's current session that nobody pressed yet,
    drawn after ``since``: what the cap counts (an expired card or a
    pressed say card never fills the chat up)."""
    with get_conn() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM chat_cards c WHERE c.chat_id = %s AND c.session_id = %s "
            "AND c.resolved_at IS NULL AND c.created_at >= %s "
            "AND NOT EXISTS (SELECT 1 FROM card_clicks k WHERE k.card_id = c.id)",
            (chat_id, session_id, since)).fetchone()["n"])


def claim_click(card_id: str, button: str, person_sub: str, options: dict, *, resolves: bool) -> dict | None:
    """The click's row, written before the action runs; None when the
    button was clicked already or, for a click that resolves the card, when
    another click resolved it first. One transaction: the card's claim
    (``resolved_at`` set only where it was NULL) and the unique click row."""
    click_id = f"click-{uuid.uuid4().hex[:12]}"
    now = _now()
    with get_conn() as conn:
        try:
            if resolves:
                claimed = conn.execute(
                    "UPDATE chat_cards SET resolved_at = %s WHERE id = %s AND resolved_at IS NULL "
                    "RETURNING id", (now, card_id)).fetchone()
                if claimed is None:
                    conn.rollback()
                    return None
            conn.execute(
                "INSERT INTO card_clicks (id, card_id, button, options, person_sub, clicked_at, status) "
                "VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)",
                (click_id, card_id, button, json.dumps(options), person_sub, now, CLICK_RUNNING))
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            return None
        conn.commit()
        return click_row(conn.execute("SELECT * FROM card_clicks WHERE id = %s", (click_id,)).fetchone())


def unclaim(click: dict, *, resolves: bool) -> None:
    """A claim given back: the click row deleted and, for a click that
    resolved the card, ``resolved_at`` cleared, so the button is pressable
    again (the message a say button queues found the queue full)."""
    with get_conn() as conn:
        conn.execute("DELETE FROM card_clicks WHERE id = %s", (click["id"],))
        if resolves:
            conn.execute("UPDATE chat_cards SET resolved_at = NULL WHERE id = %s", (click["card_id"],))
        conn.commit()


def unclaim_button(card_id: str, button: str) -> None:
    """A say button's message that left the chat's queue unsent (a Stop,
    a failed turn, a sender gate): its click row deleted and the card open
    again, so the person can press it once more."""
    with get_conn() as conn:
        gone = conn.execute("DELETE FROM card_clicks WHERE card_id = %s AND button = %s RETURNING id",
                            (card_id, button)).fetchone()
        if gone is not None:
            conn.execute("UPDATE chat_cards SET resolved_at = NULL, outcome = NULL WHERE id = %s", (card_id,))
        conn.commit()


def list_cards_in_chat(chat_id: str, since: str) -> int:
    """The list cards drawn in the chat after ``since``: the open-card
    cap's count for a kind of card that has no row of its own (its block
    is a ``chat_messages`` row under its frame's name)."""
    with get_conn() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) AS n FROM chat_messages WHERE chat_id = %s AND event_type = %s "
            "AND created_at >= %s", (chat_id, LIST_CARD_EVENT, since)).fetchone()["n"])


def finish_click(click_id: str, status: str, detail: str = "") -> None:
    with get_conn() as conn:
        conn.execute("UPDATE card_clicks SET status = %s, detail = %s WHERE id = %s",
                     (status, detail[:2000], click_id))
        conn.commit()


def set_outcome(card_id: str, outcome: dict | None) -> None:
    with get_conn() as conn:
        conn.execute("UPDATE chat_cards SET outcome = %s::jsonb WHERE id = %s",
                     (json.dumps(outcome) if outcome is not None else None, card_id))
        conn.commit()


def clicks_of(card_id: str) -> list[dict]:
    with get_conn() as conn:
        return [click_row(r) for r in conn.execute(
            "SELECT * FROM card_clicks WHERE card_id = %s ORDER BY clicked_at", (card_id,)).fetchall()]
