"""DB-history seed for fresh sessions whose on-disk context is gone.

When a chat's pinned remote machine is deleted, the retention sweep ages out
a chat's on-disk session files, or the warmup's resume gate refuses, the
CLI/Codex session that held the conversation context is unrecoverable. The
chat row is flagged via ``chats.pending_history_seed`` (set by
``remote_store.delete_remote_machine`` with ``'machine_removed:<name>'``;
the retention sweep with ``'retention'``; the warmup fresh branch with
``'resume_failed'``). The next turn — any turn: user send, TaskRunView send,
server nudge — consumes the flag at the ``_start_new_stream`` chokepoint and
prepends a compact digest of the prior conversation, rebuilt from
``chat_messages``, to the outgoing prompt, so the fresh session continues
with real context.

Direct-LLM chats never use this (they rebuild full history from the DB every
turn and are never machine-pinned). The digest is intentionally compact:
full user/assistant text (per-message capped) plus one-line ``[tool: ...]``
markers — tool RESULTS are excluded because they describe on-disk state of a
machine that no longer exists and would mislead the fresh session.
"""

import json
import logging

from storage import database as task_store

logger = logging.getLogger("claude-proxy")

# Total digest budget (~12k tokens). Retention may pass its own max_chars.
SEED_MAX_CHARS = 48_000
# One user/assistant message inside the digest.
SEED_PER_MESSAGE_CHARS = 2_500
# One [tool: ...] marker line.
SEED_TOOL_LINE_CHARS = 160

_PREAMBLE = (
    "[Context restore: this conversation previously ran in a different "
    "session. The transcript below is a digest restored from the platform's "
    "database. Files, working directories, and any other on-disk state from "
    "the previous machine/session are NOT available — verify with tools "
    "before assuming they exist.]"
)
_FOOTER = "[End of restored conversation digest. The latest message follows.]"


def _truncate(text: str, cap: int) -> str:
    text = text.strip()
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + " …[truncated]"


def _render_row(row: dict) -> str | None:
    """One digest line per DB row; None = row type not included (whitelist)."""
    role = row.get("role") or ""
    content = row.get("content") or ""
    if role == "user" and content:
        return f"User: {_truncate(content, SEED_PER_MESSAGE_CHARS)}"
    if role == "assistant" and content:
        return f"Assistant: {_truncate(content, SEED_PER_MESSAGE_CHARS)}"
    if role == "event" and (row.get("event_type") or "") == "tool":
        try:
            block = json.loads(row.get("event_data") or "{}")
        except (ValueError, TypeError):
            return None
        name = block.get("name") or ""
        if not name or name == "TodoWrite":
            return None
        detail = (block.get("summary") or "").strip()
        if not detail:
            tool_input = block.get("tool_input")
            if tool_input:
                try:
                    detail = json.dumps(tool_input, ensure_ascii=False)
                except (ValueError, TypeError):
                    detail = ""
        line = f"[tool: {name} — {detail}]" if detail else f"[tool: {name}]"
        return _truncate(line, SEED_TOOL_LINE_CHARS)
    # Everything else (thinking, permission_prompt, system, delegate_spawn /
    # delegate_result, task_spawn, workflow, metadata, bg_nudge, plan blocks,
    # todo_update, context_compact, …) is presentation/telemetry — skipped.
    return None


def build_history_seed(chat_id: str, max_chars: int = SEED_MAX_CHARS) -> str:
    """Compact conversation digest from ``chat_messages``, newest-biased.

    Walks the (already tail-capped) rows backwards accumulating rendered
    lines until ``max_chars``, then restores chronological order — a chat
    over budget loses its OLDEST lines. If the very last row is a user
    message it is skipped: at every consumption site the current turn's user
    prompt was just persisted, and it must not appear in the digest AND as
    the live prompt. (For a server turn this can drop a dangling user message
    that never got a reply — acceptable; a resend re-delivers it.)
    Returns '' when there is nothing usable to restore.
    """
    rows = task_store.get_chat_messages(chat_id)
    if rows and (rows[-1].get("role") or "") == "user":
        rows = rows[:-1]
    if not rows:
        return ""

    lines: list[str] = []
    total = 0
    for row in reversed(rows):
        line = _render_row(row)
        if line is None:
            continue
        if total + len(line) + 1 > max_chars:
            break
        lines.append(line)
        total += len(line) + 1
    if not lines:
        return ""
    lines.reverse()
    return "\n".join([_PREAMBLE, "", *lines, "", _FOOTER])


def strip_seed_prefix(text: str) -> str:
    """Return just the real prompt from a CLI user turn that may begin with a
    restored-history digest.

    :func:`consume_pending_seed` prepends ``"<digest>\\n\\n<prompt>"`` to the cold
    first prompt when reseeding an INTERACTIVE session. The CLI records the
    WHOLE submitted text as the user turn, so the interactive transcript tailers
    would otherwise persist the digest as a giant "user" message in
    ``chat_messages`` (and compound it into the next reseed). The digest always
    ends with ``_FOOTER``; keep only what follows it. Returns ``text`` unchanged
    when no digest envelope is present (the common, non-reseed case).
    """
    idx = text.rfind(_FOOTER)
    if idx == -1:
        return text
    return text[idx + len(_FOOTER):].strip()


def consume_pending_seed_digest(
    chat_id: str, max_chars: int = SEED_MAX_CHARS,
) -> tuple[str, str]:
    """Claim the chat's pending history-seed; return ``(digest, notice_message)``.

    The INTERACTIVE sibling of :func:`consume_pending_seed`: same atomic claim +
    one-time ``session_reseeded`` card, but returns the digest SEPARATELY so the
    caller can deliver it into a PTY composer as a bracketed paste rather
    than prepending it to a prompt string. ``('', '')`` when nothing is pending.
    """
    reason = task_store.claim_pending_history_seed(chat_id)
    if not reason:
        return "", ""

    kind, _, detail = reason.partition(":")
    machine_name = detail if kind in ("machine_removed", "moved") else ""
    if kind == "machine_removed":
        notice = (
            f"Original machine '{machine_name}' was removed — this chat "
            "continued with a fresh session on the agent's current target. "
            "The conversation was restored from history; files from the "
            "removed machine aren't available."
        )
    elif kind == "moved":
        # The user's explicit "Move this chat to the current target" action.
        notice = (
            f"This chat moved from {machine_name or 'its previous target'} — "
            "the session was restarted here from chat history. Files from "
            "the previous session stay where they were."
        )
    elif kind == "resume_failed":
        # The warmup's resume gate refused (missing session file, RPC
        # timeout, satellite mid-reconnect) and a fresh session took over.
        notice = (
            "The previous session could not be resumed — this chat "
            "continued with a fresh session. The conversation was restored "
            "from history; the previous session's working files may no "
            "longer be available."
        )
    else:
        # 'retention' and any unknown reason payloads.
        notice = (
            "This chat's previous session files were cleaned up — it "
            "continued with a fresh session. The conversation was restored "
            "from history; the previous session's working files may no "
            "longer be available."
        )

    digest = build_history_seed(chat_id, max_chars)
    try:
        task_store.add_chat_message(
            chat_id, "event", "",
            event_type="system",
            event_data=json.dumps({
                "type": "system",
                "subtype": "session_reseeded",
                "message": notice,
                "machine_name": machine_name,
                "reason": kind,
            }),
        )
    except Exception:
        logger.exception(
            f"history_seed: failed to persist reseed notice for chat={chat_id}"
        )
    logger.info(
        f"history_seed: claimed digest ({len(digest)} chars) for "
        f"chat={chat_id} reason={kind}"
    )
    return digest, notice


def consume_pending_seed(chat_id: str, cli_text: str) -> tuple[str, str]:
    """Claim the chat's pending seed; return ``(prompt, notice_message)``.

    The headless/``-p`` path: prepends the restored-history digest to ``cli_text``
    and persists the one-time ``session_reseeded`` card (renders as the "Continued
    with a fresh session" card on live push + every reload). No-op (``(cli_text,
    '')``) when nothing is pending. INTERACTIVE callers use
    :func:`consume_pending_seed_digest` instead (the digest is pasted into the TUI,
    not prepended to a string). The claim is atomic — concurrent turns inject once.
    """
    digest, notice = consume_pending_seed_digest(chat_id)
    if not notice:
        return cli_text, ""
    new_text = f"{digest}\n\n{cli_text}" if digest else cli_text
    return new_text, notice
