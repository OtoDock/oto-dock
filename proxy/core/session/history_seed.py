"""DB-history seed for fresh sessions whose on-disk context is gone.

When a chat's pinned remote machine is deleted, the retention sweep ages out
a chat's on-disk session files, or the warmup's resume gate refuses, the
CLI/Codex session that held the conversation context is unrecoverable. The
chat row is flagged via ``chats.pending_history_seed`` (set by
``remote_store.delete_remote_machine`` with ``'machine_removed:<name>'``;
the retention sweep with ``'retention'``; the warmup fresh branch with
``'resume_failed'``; a move whose session files could not travel with
``'moved:<move id>'`` (``services/remote/chat_move.py``; a row from before
1.9.0 holds ``'moved:<from>'``); a chat the machine cascade sent back to the
place it moved from, with turns run on the removed machine since, with
``'returned:<move id>'`` (its session resumes there; the digest carries only
those turns); the
cross-engine switch — WS ``switch_engine`` — with
``'engine_switch:<old_layer>'``). The next turn — any turn: user send,
TaskRunView send, server nudge — consumes the flag at the
``_start_new_stream`` chokepoint and
prepends a compact digest of the prior conversation, rebuilt from
``chat_messages``, to the outgoing prompt, so the fresh session continues
with real context.

Direct-LLM chats never use this (they rebuild full history from the DB every
turn and are never machine-pinned). The digest is intentionally compact:
full user/assistant text (per-message capped), one-line ``[tool: ...]``
markers and one ``[delegate result: ...]`` line per delegate's report (the
lanes' results, which reach the session as a prompt that is not a chat
row) — tool RESULTS are excluded because they describe on-disk state of a
machine that no longer exists and would mislead the fresh session.
"""

import json
import logging
from typing import cast

from core.events import tool_roles
from storage import database as task_store
from ws import wire_events as wire

logger = logging.getLogger("otodock")

# Total digest budget (~12k tokens). Retention may pass its own max_chars.
SEED_MAX_CHARS = 48_000
# One user/assistant message inside the digest.
SEED_PER_MESSAGE_CHARS = 2_500
# One [tool: ...] marker line.
SEED_TOOL_LINE_CHARS = 160
# A delegate line's agent and task name (both set by models): bounded, so
# the head never eats the line's budget for the report.
SEED_DELEGATE_NAME_CHARS = 200
# A delegate result's callback typed into a live terminal comes back from the
# transcript as a user row: its delegate line carries it instead. The
# envelope is api/tasks/delegation.py's template; the first line of a result
# that ended without a report is services/scheduler/lanes.missing_report_note's.
_RESULT_CALLBACK_PREFIXES = ("[BACKGROUND_TASK_RESULT]", "[The worker ended without a report:")

_PREAMBLE = (
    "[Context restore: this conversation previously ran in a different "
    "session. The transcript below is a digest restored from the platform's "
    "database. Files, working directories, and any other on-disk state from "
    "the previous machine/session are NOT available — verify with tools "
    "before assuming they exist.]"
)
_FOOTER = "[End of restored conversation digest. The latest message follows.]"
# A returned chat resumes its own session, which misses only the turns the
# removed machine ran.
_RETURNED_PREAMBLE = (
    "[Context restore: the turns below ran on another machine after this "
    "session's last turn there, and that machine was removed. They are "
    "restored from the platform's database. Files and any other on-disk "
    "state from that machine are NOT available. Verify with tools before "
    "assuming they exist.]"
)



def _engine_label(execution_path: str) -> str:
    """The engine's display name for the ``engine_switch:<old_layer>`` reason
    detail — from the registry, so a fourth engine names itself."""
    from core.session.session_manager import get_layer_capabilities
    caps = get_layer_capabilities(execution_path)
    return caps.display_name if caps else (execution_path or "the previous engine")


def engine_switch_notice(old_path: str) -> str:
    """The ``session_reseeded`` card text for a cross-engine switch. Shared by
    the seed-consume branch below and the ``switch_engine`` handler's
    direct-llm-target path (which persists the card at switch time — direct
    sessions never consume seeds, so the branch below would never run)."""
    old_label = _engine_label(old_path)
    return (
        f"Continued on a different AI engine — chat history was restored "
        f"from the database (previous engine: {old_label}). The old "
        "session file and its working state stay behind."
    )


def _moved_notice(detail: str) -> tuple[str, str] | None:
    """``(card text, the place it went)`` for a ``moved:<move id>`` seed, from
    the move's row; ``None`` for a legacy ``moved:<label>`` seed."""
    from storage import remote_store
    try:
        row = remote_store.get_chat_move(detail) if len(detail) == 36 else None
    except Exception:  # noqa: BLE001  # the card falls back to the legacy words
        logger.debug("history_seed: move row %s unreadable", detail[:8], exc_info=True)
        row = None
    if not row:
        return None
    from services.remote.chat_move import summary_notice
    return (summary_notice(row.get("from_label") or "its previous place",
                           row.get("to_label") or "here", row.get("why") or "",
                           source=row.get("source") or "", target=row.get("target") or ""),
            row.get("to_label") or "")


def _truncate(text: str, cap: int) -> str:
    text = text.strip()
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + " …[truncated]"


def _is_result_callback(content: str) -> bool:
    text = content.lstrip()
    if text.startswith("[Current time:"):
        # A prompt typed into a terminal can carry the time line first.
        text = text.partition("\n")[2].lstrip()
    return text.startswith(_RESULT_CALLBACK_PREFIXES)


def _delegate_line(row: dict) -> str | None:
    """A delegate's report as one line: who, which task, how it ended, the
    output's TAIL (a lane's report ends its narration) and the files it
    attached, the whole line within ``SEED_PER_MESSAGE_CHARS`` so one long
    report never crowds the rest of the digest out."""
    try:
        data = json.loads(row.get("event_data") or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    state = ("ended without a report" if data.get("report_missing")
             else (data.get("status") or "completed"))
    agent = _truncate(str(data.get("agent") or "?"), SEED_DELEGATE_NAME_CHARS)
    task_name = _truncate(str(data.get("task_name") or ""), SEED_DELEGATE_NAME_CHARS)
    head = f"[delegate result: {agent} · {task_name} · {state}]"
    paths = [f.get("path") for f in data.get("files") or []
             if isinstance(f, dict) and f.get("path")]
    budget = SEED_PER_MESSAGE_CHARS - len(head) - 1
    files = f" · files: {', '.join(cast('list[str]', paths))}" if paths else ""
    if len(files) > budget // 2:
        files = files[:budget // 2].rstrip() + " …"
    room = budget - len(files)
    output = (data.get("output_text") or "").strip()
    if len(output) > room:
        output = "…" + output[len(output) - room + 1:] if room > 1 else ""
    return f"{head} {output}{files}".rstrip()


def _render_row(row: dict) -> str | None:
    """One digest line per DB row; None = row type not included (whitelist)."""
    role = row.get("role") or ""
    content = row.get("content") or ""
    if role == "user" and content:
        if _is_result_callback(content):
            return None
        return f"User: {_truncate(content, SEED_PER_MESSAGE_CHARS)}"
    if role == "assistant" and content:
        return f"Assistant: {_truncate(content, SEED_PER_MESSAGE_CHARS)}"
    if role != "event":
        return None
    event_type = row.get("event_type") or ""
    if event_type == wire.DELEGATE_RESULT:
        return _delegate_line(row)
    if event_type == wire.PERSISTED_TOOL:
        try:
            block = json.loads(row.get("event_data") or "{}")
        except (ValueError, TypeError):
            return None
        name = block.get("name") or ""
        if not name or tool_roles.role_of(name) == tool_roles.CHECKLIST:
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
    # Everything else (thinking, permission_prompt, system, delegate_spawn,
    # task_spawn, workflow, metadata, bg_nudge, plan blocks, todo_update,
    # context_compact, …) is presentation/telemetry — skipped.
    return None


def build_history_seed(chat_id: str, max_chars: int = SEED_MAX_CHARS, *, since: str = "",
                       preamble: str = _PREAMBLE) -> str:
    """Compact conversation digest from ``chat_messages``, newest-biased.

    Walks the (already tail-capped) rows backwards accumulating rendered
    lines until ``max_chars``, then restores chronological order — a chat
    over budget loses its OLDEST lines. If the very last row is a user
    message it is skipped: at every consumption site the current turn's user
    prompt was just persisted, and it must not appear in the digest AND as
    the live prompt. (For a server turn this can drop a dangling user message
    that never got a reply — acceptable; a resend re-delivers it.)
    Returns '' when there is nothing usable to restore. ``since`` keeps only
    the rows written after that ISO time (a returned chat's turns on the
    removed machine).
    """
    rows = task_store.get_chat_messages(chat_id)
    if rows and (rows[-1].get("role") or "") == "user":
        rows = rows[:-1]
    if since:
        rows = [r for r in rows if (r.get("created_at") or "") > since]
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
    return "\n".join([preamble, "", *lines, "", _FOOTER])


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
    if kind == "returned":
        return _returned_digest(chat_id, detail, max_chars)
    machine_name = detail if kind in ("machine_removed", "moved") else ""
    moved_notice = _moved_notice(detail) if kind == "moved" else ""
    if moved_notice:
        machine_name = moved_notice[1]
    if kind == "machine_removed":
        notice = (
            f"Original machine '{machine_name}' was removed — this chat "
            "continued with a fresh session on the agent's current target. "
            "The conversation was restored from history; files from the "
            "removed machine aren't available."
        )
    elif kind == "moved" and moved_notice:
        # A move whose files could not travel (``services/remote/chat_move``):
        # its row names both places and why.
        notice = moved_notice[0]
    elif kind == "moved":
        # A move recorded before 1.9.0 (``moved:<from label>``).
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
    elif kind == "engine_switch":
        # The user's explicit cross-engine switch (switch_engine WS op);
        # detail carries the OLD layer id — mapped to a display label.
        notice = engine_switch_notice(detail)
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
            event_type=wire.SYSTEM,
            event_data=json.dumps({
                "type": wire.SYSTEM,
                "subtype": wire.SUBTYPE_SESSION_RESEEDED,
                "message": notice,
                "machine_name": machine_name,
                "reason": kind,
            }),
        )
    except Exception:
        logger.exception(
            "history_seed: failed to persist reseed notice for chat=%s", chat_id
        )
    logger.info(
        "history_seed: claimed digest (%s chars) for "
        "chat=%s reason=%s", len(digest), chat_id, kind
    )
    return digest, notice


def _returned_digest(chat_id: str, move_id: str, max_chars: int) -> tuple[str, str]:
    """The claimed ``returned:<move id>`` seed: a digest of the turns the
    chat ran on the removed machine, with its one-time card; ``('', '')``
    when there are none (the session it resumes is current)."""
    from services.remote.chat_move import returned_notice
    from storage import remote_store
    try:
        row = remote_store.get_chat_move(move_id) or {}
    except Exception:  # noqa: BLE001  # an unreadable row restores nothing
        logger.debug("history_seed: move row %s unreadable", move_id[:8], exc_info=True)
        row = {}
    since = row.get("created_at") or ""
    digest = build_history_seed(chat_id, max_chars, since=since,
                                preamble=_RETURNED_PREAMBLE) if since else ""
    if not digest:
        return "", ""
    from_label = row.get("from_label") or "its previous place"
    to_label = row.get("to_label") or "the removed machine"
    notice = returned_notice(from_label, to_label, source=row.get("source") if row else None,
                             target=row.get("target") if row else None)
    try:
        task_store.add_chat_message(
            chat_id, "event", "",
            event_type=wire.SYSTEM,
            event_data=json.dumps({
                "type": wire.SYSTEM,
                "subtype": wire.SUBTYPE_SESSION_RESEEDED,
                "message": notice,
                "machine_name": to_label,
                "reason": "returned",
            }),
        )
    except Exception:
        logger.exception("history_seed: failed to persist the returned notice for chat=%s",
                         chat_id)
    logger.info("history_seed: claimed the returned turns (%s chars) for chat=%s",
                len(digest), chat_id)
    return digest, notice


def mark_resume_refused(chat_id: str) -> None:
    """The engine refused the session a resume asked for and started a new
    one (a Codex thread it could not open): the chat's next turn continues
    from its history, with the ``resume_failed`` card, instead of losing its
    memory silently. Synchronous: the DB executor."""
    if not chat_id:
        return
    task_store.update_chat(chat_id, pending_history_seed=wire.SEED_RESUME_FAILED)
    logger.warning("history_seed: chat=%s resumed nothing (the engine opened a new session): "
                   "its next turn restores the history", chat_id[:8])


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
