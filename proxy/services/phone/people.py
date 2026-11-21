"""The people who reached an agent and their conversations, as the
Conversations tab's people view and the conversations tool read them.

An outside person is an ``external_identities`` row (``storage/identity/
external_identity_store.py``); their conversations are the phone-owned
chats whose record names them (``storage/chat/conversation_store.py``);
their memory and files are their tree (``core/session/external_identity.py``).
Forgetting a person is the caller-data sweep's job
(``services/infra/external_retention.forget_person``). Every word a caller
said reaches a reader fenced as data (``services/infra/external_data``).

Synchronous: the routes run these in a worker thread.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from adapters.phone import is_outbound
from core import layout
from core.events.common_events import ROW_ASSISTANT, ROW_USER
from core.session import external_identity
from core.session.visibility import PHONE_CHAT_OWNER
from services.infra.external_data import fence
from services.memory import memory_file
from storage import database as task_store
from storage.chat import conversation_store
from storage.identity import external_identity_store

#: The engine state a person's tree keeps for their calls: never shown, never exported.
_ENGINE_DIRS = (".claude", ".codex", ".cache")
FIRST_WORDS_CHARS = 200


def people_for_tool(agent: str) -> list[dict]:
    """The people as the conversations tool lists them, each last outcome
    fenced."""
    return [{**p, "last_outcome": fence(p["last_outcome"])} for p in people_of(agent)]


def tree_of_row(row: dict) -> Path | None:
    """A person's tree on disk, or None when it has none (or the record's
    name is not one)."""
    try:
        home = external_identity.tree_of(config.get_agent_dir(row["agent"]), row["channel"], row["slug"])
    except ValueError:
        return None
    return home if home.is_dir() else None


def words_of(chat_id: str, *, outbound: bool) -> list[dict]:
    """A conversation's words: the person's and the agent's text rows, the
    phone service's own prompts named as the line's. No tool rows (a tool's
    result may name others)."""
    from services.phone.wrapup import is_daemon_prompt
    out = []
    for row in task_store.get_chat_messages(chat_id, 2000):
        role, text = row.get("role"), (row.get("content") or "").strip()
        if not text or role not in (ROW_USER, ROW_ASSISTANT):
            continue
        who = ("line" if is_daemon_prompt(text, outbound=outbound) else "person") if role == ROW_USER else "agent"
        out.append({"who": who, "text": text, "at": row.get("created_at") or ""})
    return out


def _first_words(chat_id: str, *, outbound: bool) -> str:
    for w in words_of(chat_id, outbound=outbound):
        if w["who"] == "person":
            return w["text"][:FIRST_WORDS_CHARS]
    return ""


def _fenced_record(record: dict) -> dict:
    """A record's outcome and follow-ups as a reader's chat gets them: a
    wrap-up is a model a caller's words can steer, so what it recorded is
    fenced as the words are."""
    return {"outcome": fence(record.get("outcome") or ""),
            "follow_ups": [fence(f) for f in record.get("follow_ups") or []]}


def conversations_for_tool(agent: str, *, lines, days: int, person: str, limit: int) -> list[dict]:
    """The conversations the conversations tool lists: the most recent first,
    each with its line, person, outcome, follow-ups and the person's first
    words, fenced."""
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    rows = conversation_store.list_for_agent(
        agent, owner=PHONE_CHAT_OWNER, lines=lines, person=person, since=since, limit=limit)
    return [{
        "id": r["id"], "started": r.get("created_at") or "", "line": r.get("line") or "",
        "direction": r.get("direction") or "", "person": r.get("person_id") or "",
        **_fenced_record(r),
        "first_words": fence(_first_words(r["id"], outbound=is_outbound(r.get("direction")))),
    } for r in rows]


def conversation_for_tool(agent: str, chat_id: str, *, lines) -> dict | None:
    """One conversation for the conversations tool, or None when it is not
    one of the agent's the reader may read."""
    chat = task_store.get_chat(chat_id)
    record = conversation_store.get(chat_id)
    if (not chat or chat.get("agent") != agent or chat.get("user_sub") != PHONE_CHAT_OWNER
            or record is None or record.get("line") not in lines):
        return None
    return {
        "id": chat_id, "started": chat.get("created_at") or "", "line": record.get("line"),
        "direction": record.get("direction"), "person": record.get("identity_id") or "",
        **_fenced_record(record), "words": [{"who": w["who"], "text": fence(w["text"])} for w in words_of(chat_id, outbound=is_outbound(record.get("direction")))],
    }


def people_of(agent: str) -> list[dict]:
    """Every outside person of the agent, the most recent first, with their
    last conversation's outcome."""
    out = []
    for row in external_identity_store.list_for_agent(agent):
        records = conversation_store.for_identity(agent, row["id"])
        out.append({
            "id": row["id"], "channel": row["channel"], "number": row["external_id"],
            "first_seen": row["first_seen"], "last_seen": row["last_seen"],
            "verified": bool(row.get("verified_at")),
            "conversations": max(int(row.get("conversations") or 0), len(records)),
            "last_outcome": (records[0].get("outcome") or "") if records else "",
        })
    return out


def person_files(home: Path, *, memory: bool):
    """``(path, rel)`` of every file of a person's tree the platform shows,
    exports or names as a wrap-up's: its ``workspace/`` and ``context/``, no
    link, no engine state, no lock file; ``memory`` keeps their memory topics
    too. The one walk of a person's tree."""
    for sub in layout.USER_SUBDIRS:
        base = home / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            rel = path.relative_to(home)
            if (path.is_symlink() or not path.is_file()
                    or any(p in _ENGINE_DIRS for p in rel.parts)
                    or path.name == memory_file.LOCK_FILENAME
                    or (not memory and rel.parts[:2] == (layout.CONTEXT, memory_file.MEMORY_DIRNAME))):
                continue
            yield path, rel.as_posix()


def _files_of(home: Path) -> list[dict]:
    return [{"path": rel, "size": path.stat().st_size} for path, rel in person_files(home, memory=False)]


def _memory_of(home: Path) -> list[dict]:
    root = home / layout.CONTEXT / memory_file.MEMORY_DIRNAME
    if not root.is_dir():
        return []
    topics = []
    for path in sorted(root.glob("*.md")):
        if path.is_symlink() or not path.is_file() or path.name == "MEMORY.md":
            continue
        topics.append({"name": path.name, "text": path.read_text(encoding="utf-8", errors="replace")})
    return topics


def person_of(agent: str, identity_id: str) -> dict | None:
    """One person of the agent with their memory, their files and their
    conversations, or None when they are not the agent's."""
    row = external_identity_store.get_by_id(identity_id)
    if row is None or row.get("agent") != agent:
        return None
    home = tree_of_row(row)
    return {
        "id": row["id"], "channel": row["channel"], "number": row["external_id"],
        "first_seen": row["first_seen"], "last_seen": row["last_seen"],
        "verified": bool(row.get("verified_at")),
        "memory": _memory_of(home) if home else [],
        "files": _files_of(home) if home else [],
        "conversations": [{
            "id": r["chat_id"], "started": r.get("started_at") or "", "line": r.get("line"),
            "direction": r.get("direction"), "outcome": r.get("outcome") or "",
            "follow_ups": r.get("follow_ups") or [],
        } for r in conversation_store.for_identity(agent, identity_id)],
        "notes": conversation_store.notes_of_identity(agent, identity_id),
    }


def export_files_of(row: dict) -> list[tuple[Path, str]]:
    """``(host path, name in the export)`` of every memory topic and file of
    a person: their tree's ``workspace/`` and ``context/``, no engine state."""
    home = tree_of_row(row)
    return list(person_files(home, memory=True)) if home is not None else []


def conversation_markdown(chat_id: str, record: dict) -> str:
    """One conversation of a person's export: their words and the agent's,
    the outcome and the follow-ups."""
    lines = [f"# Conversation {record.get('started_at') or ''}", "",
             f"Line: {record.get('line') or ''}. Direction: {record.get('direction') or ''}.", ""]
    names = {"person": "Them", "agent": "The agent", "line": "Line"}
    for w in words_of(chat_id, outbound=is_outbound(record.get("direction"))):
        lines.append(f"**{names[w['who']]}**: {w['text']}")
        lines.append("")
    if record.get("outcome"):
        lines += ["## Outcome", "", record["outcome"], ""]
    if record.get("follow_ups"):
        lines += ["## Follow-ups", ""] + [f"- {f}" for f in record["follow_ups"]] + [""]
    return "\n".join(lines)
