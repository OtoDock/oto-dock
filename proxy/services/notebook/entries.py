"""Posting, reading and deleting Notebook entries.

The route decides WHO may post (the session's role) and which agent the
entry belongs to (the session token's own agent); this module owns what an
entry may say, the hourly limit, the read set's bounds and the platform
setting that budgets the prompt section.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from services.notebook.read_set import read_set
from storage.notebook import db_notebook

TEXT_MAX_CHARS = 1200
POSTS_PER_HOUR = 20
KEEP_PER_AGENT = 500
READ_LIMIT_DEFAULT = 20
READ_LIMIT_MAX = 100

INLINE_BYTES_SETTING = "notebook_inline_bytes"
INLINE_BYTES_DEFAULT = 3072
INLINE_BYTES_MAX = 16384

# What never reaches the table: C0 and C1 controls (whitespace is folded to
# one space before this runs), lone surrogates (psycopg refuses them), and
# the bidirectional and zero-width marks that could make an entry render as
# something else in another agent's prompt: the set the chat title strips
# (``api/agents/chats.py``), so ZWNJ and ZWJ (200c, 200d) stay for composite
# emoji and Persian or Indic text.
_UNSAFE = re.compile(
    "[\u0000-\u001f\u007f-\u009f\ud800-\udfff\u200b\u200e\u200f"
    "\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)


class EntryRefused(ValueError):
    """A post or a read the Notebook refuses, with the sentence to show."""


def clean_text(text: str) -> str:
    """The text as it is stored: one line, every run of whitespace one
    space, the unsafe characters gone, trimmed."""
    return _UNSAFE.sub("", " ".join((text or "").split())).strip()


def post(agent: str, author_sub: str | None, surface: str, text: str) -> dict:
    """Store an entry the agent wrote about its own work."""
    cleaned = clean_text(text)
    if not cleaned:
        raise EntryRefused("An entry needs some text.")
    if len(cleaned) > TEXT_MAX_CHARS:
        raise EntryRefused(
            f"An entry holds at most {TEXT_MAX_CHARS:,} characters. "
            "Write one to three sentences."
        )
    entry = db_notebook.insert_entry(
        uuid.uuid4().hex, agent, author_sub, surface, cleaned,
        max_per_hour=POSTS_PER_HOUR,
    )
    if entry is None:
        raise EntryRefused(
            f"This agent posted {POSTS_PER_HOUR} entries in the last hour. "
            "Post again later."
        )
    return entry


def read(agent: str, *, about: str | None = None, since: datetime | None = None,
         limit: int = READ_LIMIT_DEFAULT) -> list[dict]:
    """The read set's entries, newest first; ``about`` narrows them to one
    agent of the read set."""
    agents = read_set(agent)
    if about:
        if about not in agents:
            raise EntryRefused(
                f"{about} is not one delegation step from {agent}, so its "
                "entries are not in this Notebook."
            )
        agents = [about]
    limit = max(1, min(int(limit), READ_LIMIT_MAX))
    return db_notebook.latest(agents, limit=limit, since=since)


def inline_bytes() -> int:
    """The prompt section's budget for entry lines: the platform setting,
    its default when unset or unreadable, clamped to the allowed range."""
    try:
        from storage import database
        raw = database.get_platform_setting(INLINE_BYTES_SETTING)
    except Exception:  # noqa: BLE001 — the DB at boot: the default applies
        return INLINE_BYTES_DEFAULT
    return parse_inline_bytes(raw)


def parse_inline_bytes(raw: object) -> int:
    """A stored value read leniently: junk or nothing is the default, a
    number outside the range is clamped into it."""
    if raw is None or str(raw).strip() == "":
        return INLINE_BYTES_DEFAULT
    try:
        value = int(str(raw).strip())
    except ValueError:
        return INLINE_BYTES_DEFAULT
    return max(0, min(value, INLINE_BYTES_MAX))
