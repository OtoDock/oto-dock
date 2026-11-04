"""Shared tool-event persistence policy for the interactive transcript tailers.

Interactive tool events must be indistinguishable from the rows the headless
pump writes (``stream_pump._save_turn_blocks``): the dashboard re-renders both
through the same ``historyEventToBlock`` contract. This module holds the pieces
of that policy BOTH tailers (``transcript_tailer`` for Claude JSONL,
``codex_rollout_tailer`` for Codex rollouts) need:

- the result-content cap + one-line result summary, mirroring the PostToolUse
  hook (``hooks/tool_result_forwarder.py`` — a standalone hook script the proxy
  cannot import), which is what feeds the pump's ``tool_result`` /
  ``result_summary`` fields headless;
- the set of tools whose RESULT is never attached (the hook's skip lists —
  their cards keep the input summary only);
- the per-session pairing buffer: a transcript carries ``tool_use`` and its
  ``tool_result`` on separate lines (often in separate tail batches), so a tool
  block is held pending and persisted once its result arrives — exactly one row
  per call, keyed by the CLI's tool id. A pending block whose result never
  arrives (abort/kill) is dropped, matching the pump (an aborted tool call has
  no TOOL_RESULT and never reaches ``_turn_blocks``);
- the shared usage flush (:func:`record_batch_usage`): both tailers accumulate
  the provider tokens their batch newly consumed and write them to
  ``usage_records`` here, attributed to the serving subscription so the pool's
  headroom routing sees interactive burn;
- the progress note's row (:data:`NOTE_ROW`): the agent's words before a step,
  an assistant row with the pump's ``event_data``.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass

from core.events import tool_roles
from core.session import session_kind

logger = logging.getLogger("otodock.transcript_tool_events")

# Result-content cap before persisting — the PostToolUse hook's exact policy,
# so interactive rows carry the same truncation the pump stores headless.
RESULT_MAX_LINES = 500
RESULT_MAX_CHARS = 50000

# Tools whose result the hook never forwards headless (dedicated rendering /
# pure noise) — mirror it: persist the block, attach no result fields. By
# ROLE (the question cards, the plan-mode rows, the task list) plus the
# display MCP's own tools (the image / link itself just appeared).
SKIP_RESULT_ATTACH_ROLES = frozenset({
    tool_roles.QUESTION, tool_roles.PLAN_ENTER, tool_roles.PLAN_EXIT,
    tool_roles.TASK_READ, tool_roles.TASK_WRITE,
})
SKIP_RESULT_ATTACH_MCP = frozenset({
    "mcp__display__display_image", "mcp__display__send_url", "mcp__display__send_file",
})


# A progress note's ``event_data`` on its assistant row, byte for byte the
# pump's (``stream_pump._save_turn_blocks``): every reader keys on it alike.
NOTE_ROW = json.dumps({"note": True})


def skips_result_attach(name: str) -> bool:
    return tool_roles.role_of(name) in SKIP_RESULT_ATTACH_ROLES or name in SKIP_RESULT_ATTACH_MCP


class TailLocks:
    """Per-session mutex for the file tailers' cursor+read+persist sequence.

    Tails run in worker threads off the event loop, and several trigger paths
    (post-output debounce, pre-inject freshness tail, the 60s sweep, close, the
    Stop hook) can overlap — two threads that both read the line cursor before
    either advances it persist the same slice twice (live-observed as duplicate
    user rows). Tool/thinking rows are additionally claim-deduped, but text
    rows rely on this serialization."""

    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def acquire(self, session_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(session_id, threading.Lock())

    def forget(self, session_id: str) -> None:
        with self._guard:
            self._locks.pop(session_id, None)


# The prompts typed into a live terminal whose chat row already exists: the
# dashboard's own first prompt (persisted at send time, noted raw and then
# stamped), and the platform's prompts (a delegate result, a wake, a delegated
# task or steer: their card row is written by the site that types them). The
# CLI journals the same text and the tailer would insert it AGAIN (live-
# observed as a duplicated first user row on most fresh interactive chats,
# both layers): the tailer consumes one note per journaled line and skips
# that row; everything else about the line (turn signal, title backfill
# input) still counts.
#
# An owner-less note (the dashboard's) is one slot per chat: the stamped
# re-note of a send replaces the raw one. An owned note (a platform site's,
# the owner its terminal's session id) is one of a list: two queued prompts
# are two notes, consumed one each. A note goes on read, when the write it
# stands for did not happen (``drop_sent_prompt``), with its owner's close
# (``drop_sent_prompts``), or after an hour. The tails that consume run in
# worker threads and the notes are made on the loop: one lock.


@dataclass
class _Note:
    key: str
    noted_at: float
    owner: str


_sent_prompts: dict[str, list[_Note]] = {}
_sent_prompts_lock = threading.Lock()
_SENT_PROMPT_TTL_S = 3600.0

# The Claude TUI journals a multi-line paste wrapped in ``<pasted_content>``
# tags, the preludes inside them.
_PASTED_CONTENT_TAG_RE = re.compile(r"</?pasted_content\b[^>]{0,200}>")

# The prelude shapes as the prefix merge and the lane collection strip them
# (the stamped first prompt's time line, the viewer focus line, a task run's
# parallel-activity line, a concluded meeting's note): a satellite
# single-lines the launch-argument prompt, so a prelude there is followed by
# a space and the next line's words, not by a newline — hence ``\s*`` where
# the title regex demands the line end.
ROW_PRELUDE_RE = re.compile(
    r"^\[(?:(?:Current time: |The user is looking at the app )[^\]\n]{1,200}"
    r"|Parallel activity — [^\n]{0,2000}?\(peek_session / get_task_result\)\."
    r'|Meeting "[^\n]{0,4000}? you have only this note\.)\]\s*'
)


# The platform's own block naming a message's photos and files (the prompt
# `ws/dashboard_chat_support._process_attachments` builds), and the image
# marks the Claude TUI journals in front of a paste whose photo paths it
# turned into images (the paths leave the pasted text). Each opens on a
# literal and the tag's attributes are bounded, so a key is linear in the
# text: the send path keys every first message on the event loop.
_ATTACHMENTS_BLOCK_RE = re.compile(r"The user has attached \d+ (?:image|file)\(s\)[\s\S]*\Z")
_IMAGE_MARKS_RE = re.compile(r"^\s*(?:\[Image #\d+\]\s*)+")


def typed_key(text: str) -> str:
    """A typed prompt and its journaled line, made comparable: the paste
    tags dropped, every whitespace run folded to one space (a fresh Codex
    TUI on a satellite receives its first prompt single-lined on the argv,
    so the rollout journals ``[Current time: …] <text>`` where the note
    holds ``[Current time: …]\\n\\n<text>``). The words still have to match
    exactly, preludes included: a note holds what was typed. A message's
    attachments block is left out on both sides, with the TUI's leading
    ``[Image #N]`` marks: the TUI takes a pasted photo path in as an image,
    so its line holds the marks and not the paths."""
    plain = _PASTED_CONTENT_TAG_RE.sub("", text or "")
    plain = _ATTACHMENTS_BLOCK_RE.sub("", _IMAGE_MARKS_RE.sub("", plain))
    return " ".join(plain.split())


def prompt_key(text: str) -> str:
    """A RAW prompt and the journaled line it became, made comparable: the
    paste tags dropped, every leading prelude dropped (an interactive run's
    first prompt carries the time and parallel-activity lines ahead of the
    run's own text; the transcript keeps them, the caller holds the raw
    prompt), whitespace folded. The lane collection's and the Codex prefix
    merge's compare."""
    out = _PASTED_CONTENT_TAG_RE.sub("", text or "").strip()
    while True:
        stripped = ROW_PRELUDE_RE.sub("", out, count=1)
        if stripped == out:
            break
        out = stripped
    return " ".join(out.split())


def _prune_locked(now: float) -> None:
    for cid, notes in list(_sent_prompts.items()):
        kept = [n for n in notes if now - n.noted_at <= _SENT_PROMPT_TTL_S]
        if kept:
            _sent_prompts[cid] = kept
        else:
            _sent_prompts.pop(cid, None)


def note_sent_prompt(chat_id: str, text: str, *, owner: str = "") -> None:
    """Record a prompt typed into the chat's terminal whose row exists, so the
    tailer that later meets the same text in the CLI's journal skips
    re-persisting it (once). ``owner`` is the typing session's id for the
    platform's prompts (dropped with its close); the dashboard's own notes
    carry none and keep one slot per chat."""
    if not chat_id or not text:
        return
    now = time.time()
    with _sent_prompts_lock:
        _prune_locked(now)
        notes = _sent_prompts.setdefault(chat_id, [])
        if not owner:
            notes[:] = [n for n in notes if n.owner]
        notes.append(_Note(typed_key(text), now, owner))
    logger.debug("sent-prompt note chat=%s len=%d owner=%s", chat_id[:8], len(text), owner[:8])


def consume_sent_prompt(chat_id: str, text: str) -> bool:
    """True exactly once per note for a journaled line whose key matches one
    of the chat's notes (the oldest first) — the caller skips persisting that
    user row. A line that matches no note leaves every note in place."""
    key = typed_key(text)
    with _sent_prompts_lock:
        notes = _sent_prompts.get(chat_id) or []
        for i, note in enumerate(notes):
            if note.key == key:
                del notes[i]
                if not notes:
                    _sent_prompts.pop(chat_id, None)
                logger.debug("sent-prompt consumed chat=%s len=%d", chat_id[:8], len(text))
                return True
        pending = len(notes)
    if pending:
        # A line that matches none of the chat's notes while notes wait is
        # the shape drifting somewhere between the delivery and the journal,
        # or a person's own line: say so in the debug log.
        logger.debug("sent-prompt mismatch chat=%s notes=%d journal_len=%d",
                     chat_id[:8], pending, len(text or ""))
    return False


def drop_sent_prompt(chat_id: str, text: str, *, owner: str) -> bool:
    """The write a note stood for did not happen: the newest note of that
    owner with that key goes. True when one went."""
    key = typed_key(text)
    with _sent_prompts_lock:
        notes = _sent_prompts.get(chat_id) or []
        for i in range(len(notes) - 1, -1, -1):
            if notes[i].owner == owner and notes[i].key == key:
                del notes[i]
                if not notes:
                    _sent_prompts.pop(chat_id, None)
                return True
    return False


def drop_sent_prompts(chat_id: str, *, owner: str) -> int:
    """The owner's terminal closed: every note it made and the journal never
    gave back goes (its final tail was the last chance). The dashboard's
    owner-less notes stay. Returns how many went."""
    if not owner:
        return 0
    with _sent_prompts_lock:
        notes = _sent_prompts.get(chat_id) or []
        kept = [n for n in notes if n.owner != owner]
        gone = len(notes) - len(kept)
        if kept:
            _sent_prompts[chat_id] = kept
        else:
            _sent_prompts.pop(chat_id, None)
    if gone:
        logger.debug("sent-prompt dropped chat=%s owner=%s n=%d", chat_id[:8], owner[:8], gone)
    return gone


def persist_event(task_store, chat_id: str, block: dict) -> None:
    """One pump-shaped event row. Default json.dumps separators are load-bearing:
    ``get_last_todo_snapshot`` LIKE-matches ``"name": "TodoWrite"``."""
    task_store.add_chat_message(
        chat_id, "event", "",
        event_type=block["type"], event_data=json.dumps(block),
    )


def extract_result_text(content) -> str:
    """Text of a tool result's ``content`` (str or content-block list).

    Only ``text`` blocks are read — an image result's base64 payload must never
    be persisted into ``chat_messages``."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def truncate_result(text: str) -> str:
    """Cap result content (500 lines, then 50KB) with the hook's markers."""
    if not text:
        return text
    lines = text.split("\n")
    if len(lines) > RESULT_MAX_LINES:
        text = "\n".join(lines[:RESULT_MAX_LINES]) + \
            f"\n... ({len(lines) - RESULT_MAX_LINES} more lines)"
    if len(text) > RESULT_MAX_CHARS:
        text = text[:RESULT_MAX_CHARS] + "\n... (truncated)"
    return text


def result_summary(tool_name: str, result_text: str) -> str:
    """One-line result summary — the hook's ``_extract_summary`` heuristics,
    by role."""
    role = tool_roles.role_of(tool_name)
    if role == tool_roles.SHELL:
        lines = result_text.count("\n") + 1 if result_text.strip() else 0
        return f"{lines} lines" if lines else "ok"
    if role == tool_roles.SEARCH:
        if not result_text.strip():
            return "no matches"
        return f"{len([l for l in result_text.strip().splitlines() if l.strip()])} results"
    if role == tool_roles.GLOB:
        if not result_text.strip():
            return "no files"
        return f"{len([l for l in result_text.strip().splitlines() if l.strip()])} files"
    if role == tool_roles.READ:
        if not result_text.strip():
            return "empty file"
        return f"{result_text.count(chr(10)) + 1} lines"
    if role == tool_roles.WRITE:
        if "error" in result_text.lower()[:100]:
            first_line = result_text.strip().splitlines()[0] if result_text.strip() else ""
            return f"error: {first_line[:80]}"
        return "ok"
    if not result_text.strip():
        return "ok"
    first_line = result_text.strip().splitlines()[0]
    if "error" in first_line.lower()[:100]:
        return f"error: {first_line[:80]}"
    return "ok"


def attach_result(block: dict, result_text: str, *, is_error: bool = False) -> dict:
    """Attach a (truncated) result to a pending tool block — unless the tool is
    in the hook's skip set, in which case the block persists input-only."""
    if skips_result_attach(block.get("name", "")):
        return block
    block["tool_result"] = truncate_result(result_text)
    block["result_summary"] = result_summary(block.get("name", ""), result_text)
    block["is_error"] = bool(is_error)
    return block


def record_batch_usage(session_id: str, chat_id: str, usage_by_model: dict,
                       task_store) -> int:
    """Write one tail batch's provider usage to ``usage_records`` — one row per
    model in ``usage_by_model`` (each value: input_tokens/output_tokens/
    cache_read/cache_write sums for tokens NEWLY consumed this batch; the
    caller owns the dedupe that makes "newly" true). Returns rows written
    (0 on any failure: usage accounting must never break transcript
    persistence, hence the blanket except). Shared by both tailers — the
    ONLY ``usage_records`` writers for interactive sessions (the pump records
    headless turns; interactive has no pump).

    Attribution mirrors the pump's ``_record_usage``: ``source_key`` is the
    subscription that served the session (the pool's binding, ``'default'``
    when unbound — e.g. admin OAuth outside the pool), scope follows the chat
    row's agent (Shared-only → agent scope). Cost is computed from
    ``config.price_usage`` (the model's rates: DB → registry → provider
    default, a long-context tier applied) because the CLIs only emit a cost
    figure on headless result events — and ``message_count=1``
    keeps a $0 row through ``record_turn_usage``'s filter (pump parity: token
    counts survive even when pricing resolves to zero). Batch granularity (not
    turn) is deliberate: pool routing consumes a SUM, and a batch is the
    natural idempotency unit the tail cursors already guarantee. Known
    residual: subagent (sidechain) usage rides separate transcript files the
    tailers never open, so it is not counted. A model key of ``""`` falls back
    to the chat row's model (Codex token_count events carry no model; the
    session's ``turn_context`` usually names it first)."""
    try:
        rec = task_store.get_chat(chat_id) or {}
        if not rec:
            logger.warning(
                "interactive %s: usage batch dropped — no chat row for %s",
                session_id[:8], chat_id)
            return 0
        from config import get_model_provider, price_usage
        from core.session import visibility as _vis
        from core.session.session_state import get_session_client_type
        from services.billing import usage_service
        from services.engines import subscription_pool

        agent = rec.get("agent", "")
        scope = "agent" if _vis.is_shared_only(agent) else "user"
        sub_id = subscription_pool.get_session_subscription(session_id) or "default"
        user_sub = rec.get("user_sub")
        # Pump parity: a Shared-only agent's human session runs on the
        # interacting USER's subscription — bill the payer (real person),
        # not the synthetic agent::{slug} chat owner. Agent-scope stays
        # only for platform-paid sessions (no payer bound).
        if scope == "agent":
            payer = subscription_pool.get_session_payer_sub(session_id)
            if payer:
                user_sub = payer
                scope = "user"
        rows: list[dict] = []
        for model, acc in usage_by_model.items():
            if not any((acc["input_tokens"], acc["output_tokens"],
                        acc["cache_read"], acc["cache_write"])):
                continue  # synthetic zero-usage lines — nothing to bill
            if not model:
                model = rec.get("model") or ""
            provider = get_model_provider(model) or "anthropic"
            cost = price_usage(
                model, provider,
                input_tokens=acc["input_tokens"], output_tokens=acc["output_tokens"],
                cache_write_tokens=acc["cache_write"], cache_read_tokens=acc["cache_read"])
            rows.append({
                "user_sub": user_sub,
                "agent": agent,
                "scope": scope,
                # The DRIVER's kind (the session's, not the row's — pump
                # parity): an interactive task run bills as a task turn, a
                # person's session as a chat turn; "interactive" only for a
                # row that carries no kind at all.
                "source_type": (session_kind.driver_source_type(
                                    get_session_client_type(session_id), rec)
                                if rec.get("source_type") else "interactive"),
                "source_id": chat_id,
                "cost_usd": max(0.0, round(cost, 6)),
                "input_tokens": acc["input_tokens"],
                "output_tokens": acc["output_tokens"],
                "cache_read": acc["cache_read"],
                "cache_write": acc["cache_write"],
                "message_count": 1,
                "provider": provider,
                "model": model,
                "source_key": sub_id,
            })
        if rows:
            usage_service.record_turn_usage(rows)
        return len(rows)
    except Exception:
        logger.exception("interactive usage recording failed for %s", session_id[:8])
        return 0


class ToolEventBuffer:
    """One session's tool_use→tool_result pairing state across tail batches.

    ``seen`` is the per-session dedupe: a tool id is claimed exactly once
    (``dict.setdefault`` — atomic under the GIL, so concurrent close/sweep
    tails can't both claim it), whether the block ends up pending, persisted,
    or deliberately skipped."""

    def __init__(self) -> None:
        self.pending: dict[str, dict] = {}
        self._seen: dict[str, object] = {}
        # The dialog calls (a question, a plan approval) seen and not yet
        # answered, across batches: their answer is the one result that
        # reopens a turn the dialog parked.
        self.dialogs: set[str] = set()

    def claim(self, key: str) -> bool:
        """Claim a tool id for processing; False if already claimed. A unique
        token + one ``setdefault`` keeps the claim a single atomic dict op."""
        token = object()
        return self._seen.setdefault(key, token) is token

    def open(self, key: str, block: dict) -> bool:
        """Buffer a block until its result arrives. False on a duplicate id."""
        if not self.claim(key):
            return False
        self.pending[key] = block
        return True

    def close(self, key: str) -> dict | None:
        """Pop the pending block for a result's tool id (None if unknown —
        the call predates this buffer or its input line was never seen)."""
        return self.pending.pop(key, None)
