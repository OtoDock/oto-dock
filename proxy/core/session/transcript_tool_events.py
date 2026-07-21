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
  headroom routing sees interactive burn.
"""
from __future__ import annotations

import json
import logging
import threading

logger = logging.getLogger("claude-proxy.transcript_tool_events")

# Result-content cap before persisting — the PostToolUse hook's exact policy,
# so interactive rows carry the same truncation the pump stores headless.
RESULT_MAX_LINES = 500
RESULT_MAX_CHARS = 50000

# Tools whose result the hook never forwards headless (dedicated rendering /
# pure noise) — mirror it: persist the block, attach no result fields.
SKIP_RESULT_ATTACH = frozenset({
    "AskUserQuestion", "Task", "EnterPlanMode", "ExitPlanMode",
    "TaskCreate", "TaskUpdate", "TaskList", "TaskGet", "TaskStop", "TaskOutput",
    "mcp__display__display_image", "mcp__display__send_url", "mcp__display__send_file",
})


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


# chat_id → (prompt text, noted_at): the ONE user prompt the dashboard warmup
# already persisted at send-time (_persist_first_prompt — durability during
# the spawn window + immediate sidebar title). The CLI then journals the SAME
# text and the tailer would insert it AGAIN — live-observed as a duplicated
# first user row on most fresh interactive chats, both layers. The tailer
# consumes the note once and skips that single row; everything else about the
# line (turn signal, title backfill input) still counts. One-shot + TTL-pruned
# (a note whose spawn failed must not linger and swallow a future prompt; the
# [Current time:] prelude makes accidental text collisions across turns
# practically impossible anyway).
_sent_prompts: dict[str, tuple[str, float]] = {}
_SENT_PROMPT_TTL_S = 3600.0


def note_sent_prompt(chat_id: str, text: str) -> None:
    """Record a send-time-persisted user prompt so the tailer that later meets
    the same text in the CLI's journal skips re-persisting it (once)."""
    if not chat_id or not text:
        return
    import time as _time
    now = _time.time()
    for cid, (_, ts) in list(_sent_prompts.items()):
        if now - ts > _SENT_PROMPT_TTL_S:
            _sent_prompts.pop(cid, None)
    _sent_prompts[chat_id] = (text, now)


def consume_sent_prompt(chat_id: str, text: str) -> bool:
    """True exactly once for the noted (chat, text) pair — the caller skips
    persisting that user row. Non-matching text leaves the note in place
    (the journaled first prompt should be byte-identical to what was sent)."""
    rec = _sent_prompts.get(chat_id)
    if rec and rec[0] == text:
        _sent_prompts.pop(chat_id, None)
        return True
    return False


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
    """One-line result summary — the hook's ``_extract_summary`` heuristics."""
    if tool_name == "Bash":
        lines = result_text.count("\n") + 1 if result_text.strip() else 0
        return f"{lines} lines" if lines else "ok"
    if tool_name == "Grep":
        if not result_text.strip():
            return "no matches"
        return f"{len([l for l in result_text.strip().splitlines() if l.strip()])} results"
    if tool_name == "Glob":
        if not result_text.strip():
            return "no files"
        return f"{len([l for l in result_text.strip().splitlines() if l.strip()])} files"
    if tool_name == "Read":
        if not result_text.strip():
            return "empty file"
        return f"{result_text.count(chr(10)) + 1} lines"
    if tool_name in ("Write", "Edit"):
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
    if block.get("name") in SKIP_RESULT_ATTACH:
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
    ``get_model_pricing`` (DB → registry → provider default) because the CLIs
    only emit a cost figure on headless result events — and ``message_count=1``
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
        from core.session import visibility as _vis
        from services.billing import usage_service
        from services.engines import subscription_pool
        from config import get_model_pricing, get_model_provider

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
            p_in, p_out, p_cw, p_cr = get_model_pricing(model, provider)
            cost = (acc["input_tokens"] * p_in + acc["output_tokens"] * p_out
                    + acc["cache_write"] * p_cw + acc["cache_read"] * p_cr
                    ) / 1_000_000
            rows.append({
                "user_sub": user_sub,
                "agent": agent,
                "scope": scope,
                "source_type": rec.get("source_type") or "interactive",
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
