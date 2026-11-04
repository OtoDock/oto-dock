"""The context of a turn that ended short, read back from the chat's rows:
what the person said and what the agent had answered, for the engine
that lost it (a hard Stop, an exited, silent or lost turn). The next
message carries it ahead of its own text."""

import json

from core.events.common_events import (
    ROW_ASSISTANT,
    ROW_EVENT,
    ROW_USER,
    row_attachments,
)
from storage import database as task_store
from ws import wire_events as wire


def _person_row(msg: dict) -> tuple[str, list[str], list[str]] | None:
    """A person's row as the context reads it: its text and its photos' and
    files' paths. None for another row, or a person's row with nothing in it."""
    if msg["role"] != ROW_USER:
        return None
    photos, files = row_attachments(msg.get("event_data"))
    text = msg["content"] or ""
    return (text, photos, files) if (text or photos or files) else None


def cancelled_context(cid: str, *, new_text: str | None = None, new_rows: int = 1,
                      inline_photos: bool = False) -> str:
    """Read the cancelled turn's messages from DB and format for injection.

    The current user message was JUST saved before this is called (with
    the person's waiting messages an idle send took: ``new_rows`` rows
    in all), unless ``new_text`` names it (a queued batch whose rows land
    after its turn starts). Walk backwards past them to find the previous
    user message (the cancelled one, with the rows that went with it in one
    batch) and any partial assistant response after it.

    A message of photos or files alone is a message. The cut message's
    attachments are named by their paths (an engine that takes photos
    inline, ``inline_photos``, gets their count), so Resume, which sends
    none, still gives the agent what the turn was about.
    """
    messages = task_store.get_chat_messages(cid)
    if not messages:
        return ""

    skip = 0 if new_text is not None else max(1, new_rows)
    user_count = 0 if new_text is None else 1
    # The new message's attachments, known when its row is read here.
    new_attached: set[str] | None = None if new_text is not None else set()
    read_new = False
    new_text = new_text or ""
    cut: list[tuple[str, list[str], list[str]]] = []  # oldest first
    assistant_parts: list[str] = []
    # The turn ended on its own (the chat's ``turn_ended`` row), not by
    # the person's Stop: the words say so.
    ended = ""

    for msg in reversed(messages):
        person = _person_row(msg)
        if cut:
            # The rows that went with the cut message (the person's waiting
            # messages it took along) sit right above it.
            if person is None or person[0].strip() == wire.RESUME_TEXT:
                break
            cut.insert(0, person)
            continue
        if person is not None:
            text, photos, files = person
            if skip:
                skip -= 1
                if new_attached is not None and not read_new:
                    new_text, new_attached, read_new = text, set(photos + files), True
                if not skip:
                    user_count = 1
                continue  # Skip the new message(s) (just saved)
            if text.strip() == wire.RESUME_TEXT:
                # An earlier Resume: part of the same work, never the
                # prompt that started it.
                continue
            cut.insert(0, person)
        elif msg["role"] == ROW_ASSISTANT and msg["content"]:
            if user_count >= 1:
                assistant_parts.insert(0, msg["content"])
        elif msg["role"] == ROW_EVENT:
            if user_count >= 1 and not ended and msg.get("event_type") == wire.SYSTEM:
                try:
                    data = json.loads(msg.get("event_data") or "{}")
                except (TypeError, ValueError):
                    data = {}
                if isinstance(data, dict) and data.get("subtype") == wire.SUBTYPE_TURN_ENDED:
                    ended = str(data.get("reason") or "error")
            continue

    if not cut:
        return ""
    said = "\n\n".join(text for text, _p, _f in cut if text)
    photos = [p for _t, ps, _f in cut for p in ps]
    files = [f for _t, _p, fs in cut for f in fs]
    if new_text.strip() == said.strip() and (
            new_attached is None or new_attached <= set(photos + files)):
        # The same message again (the card's Send again, its photos that
        # could still be read): the engine gets it once, with no copy of the
        # turn it repeats.
        return ""

    head = (f"[Your previous turn ended early ({ended}) before it finished. "
            if ended else "[Your previous response was cancelled by the user. ")
    parts = [
        head + "The cancelled turn was not saved to your session context, "
        "so here is what happened:]",
        f"User said: {said}" if said else "User sent attachments with no text.",
    ]
    named = files if inline_photos else photos + files
    if inline_photos and photos:
        parts.append(f"Attached to it: {len(photos)} photo(s).")
    if named:
        parts.append("Attached to it (open them with your Read tool):\n"
                     + "\n".join(f"- /{path}" for path in named))
    if assistant_parts:
        combined = "\n".join(assistant_parts)
        if len(combined) > 2000:
            combined = combined[:2000] + "\n... (response was truncated)"
        parts.append(f"Your partial response before cancellation:\n{combined}")
    else:
        parts.append("(You had not started responding yet when cancelled)")
    parts.append("[End of cancelled context. The user's new message follows below.]")

    return "\n".join(parts)
