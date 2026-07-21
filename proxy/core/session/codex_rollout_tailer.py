"""Persist an interactive Codex CLI session's rollout JSONL into chat_messages.

The Codex twin of :mod:`core.session.transcript_tailer` (which is Claude-JSONL-specific).
Interactive sessions don't flow through the pump, so
nothing writes their turns to the DB. On output-quiet / close / the idle-reaper sweep
the proxy reads the Codex **rollout** JSONL and appends new user/assistant text to
``chat_messages`` for dashboard visibility, and captures the thread id into
``chats.codex_thread_id`` so the existing ``codex resume <id>`` path can continue the
chat after a kill/reopen.

Rollout format (``$CODEX_HOME/sessions/<Y>/<M>/<D>/rollout-<ts>-<uuid>.jsonl``): each
line is ``{timestamp, type, payload}``. We read:
  - ``session_meta`` (line 0) → ``payload.id`` is the thread id; ``payload.cwd`` /
    ``originator`` / ``thread_source`` discriminate the right rollout.
  - ``response_item`` with ``payload.type == "message"`` → a turn message:
    ``payload.role`` (user|assistant|developer) + ``payload.content[]`` blocks of
    ``{type: input_text|output_text, text}``.
We KEEP user + assistant text and SKIP: the ``developer`` perms message, the synthetic
first ``user`` message (the AGENTS.md / ``<environment_context>`` / ``<INSTRUCTIONS>``
injection), and everything that isn't a message (reasoning, tool calls, events).

CODEX_HOME is per-(user, agent) scope — shared across that scope's chats — so, unlike
the Claude tailer (which finds ``<session_id>.jsonl`` by name), this DISCOVERS THIS
session's rollout (originator==``codex-tui``, thread_source!=``subagent``, cwd match,
created after spawn) and PINS it, excluding rollouts already pinned by sibling
sessions. Persistence is idempotent across resume + proxy restart via a prefix-merge
against already-persisted messages on the first tail (the line cursor handles
subsequent tails within a session).

Tool + reasoning rollout items ALSO persist, as the same ``role="event"`` rows the
headless pump writes (mirroring ``transcript_tailer`` — see
``transcript_tool_events`` for the shared shape/policy): ``function_call`` /
``custom_tool_call`` and their ``*_output`` twins pair through a per-session
``ToolEventBuffer`` (keyed on ``call_id``; a call whose output never lands is
dropped, like an aborted headless tool) into one ``tool`` block each —
``exec_command`` renders as ``Bash`` and ``update_plan`` as a ``TodoWrite``
checklist snapshot, the headless display names; ``tool_search_call`` /
``web_search_call`` map to ``ToolSearch`` / ``web_search``; the delegation MCP's
``delegate`` is skipped (the delegation endpoint persists its own
``delegate_spawn`` row); ``reasoning`` summaries persist as ``thinking`` rows;
``request_user_input`` persists an immediate ``question`` row (its answer output
is never attached) and, while unanswered at batch end, folds the batch to a
turn close with ``question_pending=True`` — the Claude ``AskUserQuestion``
parity (see ``transcript_tailer`` + SESSIONS.md "AskUserQuestion parks the turn").
The first-tail backstop covers event rows too: already-persisted tool ids /
thinking texts are collected next to the text prefix and skipped on a
post-restart re-read from line 0.

Like the Claude tailer, this is also the ONLY ``usage_records`` writer for
interactive Codex sessions: ``event_msg``/``token_count`` events carry the per
API call's ``last_token_usage``, summed per batch and flushed through the
shared ``transcript_tool_events.record_batch_usage`` (model from the last
``turn_context``; a post-restart line-0 re-read calibrates without recording —
see ``_process_rollout_lines``).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re

from core.session.history_seed import strip_seed_prefix
from core.session.transcript_tool_events import (
    TailLocks, ToolEventBuffer, attach_result, consume_sent_prompt,
    persist_event, record_batch_usage,
)

logger = logging.getLogger("claude-proxy.codex_rollout_tailer")

# session_id → number of rollout lines already processed. The rollout JSONL is
# append-only within a session, so a line offset is a sufficient, cheap cursor.
_offsets: dict[str, int] = {}
# session_id → tool call/output pairing + dedupe state (see ToolEventBuffer).
_tool_events: dict[str, ToolEventBuffer] = {}
# Serializes cursor+read+persist per session — overlapping tail triggers run in
# separate threads and would otherwise persist the same slice twice.
_tail_locks = TailLocks()
# session_id → resolved rollout path (pinned on first successful resolve so a shared
# CODEX_HOME's other rollouts can't be picked up later, and sibling sessions exclude
# each other's pins).
_resolved: dict[str, str] = {}
# session_ids whose thread id has already been written to chats.codex_thread_id.
_thread_saved: set[str] = set()
# session_id → the model named by the last `turn_context` line seen — the
# usage-row model (token_count events carry no model). Falls back to the chat
# row's model in record_batch_usage when no turn_context arrived yet.
_usage_models: dict[str, str] = {}

# Fresh-spawn lower bound slack: Codex writes its rollout within ~1 s of spawn; allow
# clock/race slack so we never miss it but still exclude a sibling chat's older rollout.
_RESOLVE_MTIME_MARGIN_S = 5.0

# Read a generous prefix on resume — interactive chats never approach this, and a
# truncated prefix would mis-align the merge.
_PREFIX_LIMIT = 10000


def seek_past_existing(session_id: str, chat_id: str = "") -> int:
    """Initialize the tail cursor at the rollout's CURRENT end.

    Called at interactive ATTACH, before the CLI starts — the Codex twin of
    ``transcript_tailer.seek_past_existing``. Everything already in the rollout
    at attach time was persisted by the pump or an earlier stint's tails, so
    tailing it from line 0 would duplicate the conversation. The first-tail
    ``_persisted_prefix`` dedup below stays as the backstop for post-restart
    re-reads (where this seek never ran).
    """
    path = resolve_rollout_path(session_id, chat_id)
    n = 0
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                n = sum(1 for _ in fh)
        except Exception:
            logger.exception("seek_past_existing failed for %s", session_id[:8])
            n = 0
    _offsets[session_id] = n
    _tool_events[session_id] = ToolEventBuffer()
    if n:
        logger.info(
            "interactive %s: rollout seek past %d pre-existing line(s)",
            session_id[:8], n,
        )
    return n


def forget(session_id: str) -> None:
    """Drop all per-session state (called on interactive-session close).
    Still-pending tool blocks drop with it — a call whose output never arrived
    isn't persisted headless either."""
    _offsets.pop(session_id, None)
    _resolved.pop(session_id, None)
    _thread_saved.discard(session_id)
    _tool_events.pop(session_id, None)
    _usage_models.pop(session_id, None)
    _tail_locks.forget(session_id)


def rollout_exists(host_codex_home: str, thread_id: str) -> bool:
    """True if the thread's rollout JSONL is still on disk under this CODEX_HOME.

    ``codex resume <thread_id>`` needs the rollout, so this decides whether an
    interactive Codex spawn resumes the thread or starts fresh. The thread + its
    rollout persist independently of the in-memory app-server session, so a
    ``-p`` ⇄ terminal switch (which closes the in-memory session) can still
    resume the same conversation."""
    if not host_codex_home or not thread_id:
        return False
    pattern = os.path.join(host_codex_home, "sessions", "**", f"rollout-*-{thread_id}.jsonl")
    return bool(glob.glob(pattern, recursive=True))


def _read_session_meta(path: str) -> dict | None:
    """Parse the first line (session_meta payload) of a rollout, or None."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
        obj = json.loads(first)
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if obj.get("type") == "session_meta":
        return obj.get("payload") or {}
    return None


def resolve_rollout_path(session_id: str, chat_id: str) -> str | None:
    """Locate THIS interactive Codex session's rollout JSONL under its CODEX_HOME.

    Pins on first success. On resume (the chat carries a ``codex_thread_id``) prefers
    the thread's own ``rollout-*-<tid>.jsonl`` (``codex resume`` appends to it);
    otherwise picks the newest ``codex-tui`` rollout matching the sandbox CWD that was
    created after spawn — never an app-server / exec / subagent rollout, never one
    already pinned by another live session."""
    pinned = _resolved.get(session_id)
    if pinned and os.path.isfile(pinned):
        return pinned

    from core.session.session_state import get_session_codex_dir
    ctx = get_session_codex_dir(session_id)
    if not ctx:
        return None
    home = ctx.get("home") or ""
    cwd = ctx.get("cwd") or ""
    started_at = ctx.get("started_at") or 0.0
    sessions_dir = os.path.join(home, "sessions")
    if not os.path.isdir(sessions_dir):
        return None

    from storage import database as task_store
    try:
        rec = task_store.get_chat(chat_id)
        tid = (rec or {}).get("codex_thread_id", "") or ""
    except Exception:
        tid = ""

    others = {p for s, p in _resolved.items() if s != session_id}
    candidates = glob.glob(
        os.path.join(sessions_dir, "**", "rollout-*.jsonl"), recursive=True
    )

    # Resume: the thread's own rollout (codex resume <tid> appends to it).
    if tid:
        suffix = f"-{tid}.jsonl"
        for path in candidates:
            if path in others or not os.path.basename(path).endswith(suffix):
                continue
            meta = _read_session_meta(path)
            if meta and meta.get("thread_source") != "subagent":
                _resolved[session_id] = path
                return path

    # Fresh (or a resume whose rollout wasn't found by name): the newest codex-tui
    # rollout for this CWD created after spawn.
    best: str | None = None
    best_mtime = -1.0
    for path in candidates:
        if path in others:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if not tid and mtime < started_at - _RESOLVE_MTIME_MARGIN_S:
            continue
        meta = _read_session_meta(path)
        if not meta:
            continue
        if meta.get("originator") != "codex-tui":
            continue
        if meta.get("thread_source") == "subagent":
            continue
        if cwd and meta.get("cwd") != cwd:
            continue
        if mtime > best_mtime:
            best, best_mtime = path, mtime
    if best:
        _resolved[session_id] = best
        return best
    return None


def resolve_and_tail(session_id: str, chat_id: str) -> dict:
    """Discover the rollout path (no hook dependency) and tail it.

    Called on interactive-session close (so a killed/reaped/toggled-off chat persists
    its conversation) and periodically by the idle-reaper sweep (so a proxy crash
    can't lose the turns)."""
    path = resolve_rollout_path(session_id, chat_id)
    if not path:
        return {"persisted": 0, "reason": "no_rollout"}
    return tail_rollout(session_id, chat_id, path)


_SYNTHETIC_USER_PREFIXES = ("# AGENTS.md instructions", "<user_instructions>",
                            "<turn_aborted>")


def _is_synthetic_user(text: str) -> bool:
    """True for Codex's injected user-role messages (the AGENTS.md instructions /
    environment-context / user-instructions blocks) — not real user turns."""
    s = (text or "").lstrip()
    if s.startswith(_SYNTHETIC_USER_PREFIXES):
        return True
    return "<environment_context>" in s


def _extract_text(content) -> str:
    """Concatenate the text of a Codex message's content (str or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict)
            and b.get("type") in ("input_text", "output_text", "text")
        )
    return ""


# Injected `[Current time: ...]` stamp line(s) — twin of
# ``transcript_tailer._TIME_PRELUDE_RE`` (start-anchored exact shape only).
_TIME_PRELUDE_RE = re.compile(r"^\[Current time: [^\]\n]{1,160}\][ \t]*(?:\r?\n+|$)")


def _title_from_prompt(text: str) -> str:
    """Deterministic chat title from the first user message — mirrors
    ``transcript_tailer._title_from_prompt`` / ``ws.dashboard._deterministic_title``
    (first ~6 words / 48 chars, whitespace-collapsed, ellipsis if truncated).
    Injected ``[Current time: ...]`` stamps are dropped first; a prompt with
    nothing user-authored left returns "" so the chat stays untitled for the
    next real prompt (callers skip the empty title)."""
    out = text or ""
    while True:
        stripped = _TIME_PRELUDE_RE.sub("", out, count=1)
        if stripped == out:
            break
        out = stripped
    cleaned = " ".join(out.split())
    if not cleaned:
        return ""
    words = cleaned.split(" ")
    title = " ".join(words[:6])
    cut = len(words) > 6
    if len(title) > 48:
        title = title[:48].rstrip()
        cut = True
    return title + ("…" if cut else "")


def _persisted_prefix(chat_id: str) -> tuple[list[tuple[str, str]], set[str]]:
    """Already-persisted history for this chat — what a resumed/restarted tail
    must skip so it doesn't double-insert. Two parts from one row fetch: the
    chronological (role, content) TEXT prefix (consumed sequentially by the
    merge), and the EVENT keys — ``tool:<tool_id>`` / ``thinking:<content>`` —
    matched by membership (tool ids are unique; a rollout re-read revisits each
    exactly once)."""
    from storage import database as task_store
    try:
        rows = task_store.get_chat_messages(chat_id, limit=_PREFIX_LIMIT)  # newest-first
    except Exception:
        return [], set()
    out: list[tuple[str, str]] = []
    event_keys: set[str] = set()
    for r in reversed(rows):
        role = r.get("role")
        content = r.get("content") or ""
        if role in ("user", "assistant") and content:
            out.append((role, content))
        elif role == "event" and r.get("event_data"):
            try:
                evt = json.loads(r["event_data"])
            except (json.JSONDecodeError, ValueError, TypeError):
                continue
            if r.get("event_type") == "thinking":
                event_keys.add("thinking:" + (evt.get("content") or ""))
            else:
                tid = evt.get("tool_id") or evt.get("tool_use_id") or ""
                if tid:
                    event_keys.add("tool:" + tid)
    return out, event_keys


def tail_rollout(session_id: str, chat_id: str, rollout_path: str) -> dict:
    """Process rollout lines added since the last tail (LOCAL file path).

    Reads the on-disk rollout JSONL from the per-session line cursor and hands the
    new lines to :func:`_process_rollout_lines`. On the FIRST tail it also computes
    the already-persisted prefix so a resumed / post-restart chat (which re-reads
    the rollout from line 0) doesn't double-insert history. Idempotent via the
    cursor; returns simple stats.
    """
    if not rollout_path or not os.path.isfile(rollout_path):
        return {"persisted": 0, "reason": "no_file"}

    with _tail_locks.acquire(session_id):
        first_tail = session_id not in _offsets
        # On the first tail of a session, skip messages already in the DB (a resumed
        # or post-restart chat re-reads the rollout from line 0). ``prefix[ptr]`` is
        # the next persisted message we expect to re-encounter; matches are consumed,
        # not re-added. ``known_events`` is the same backstop for tool/thinking rows.
        prefix, known_events = _persisted_prefix(chat_id) if first_tail else ([], set())

        start = _offsets.get(session_id, 0)
        processed = start
        new_lines: list[str] = []
        try:
            with open(rollout_path, "r", encoding="utf-8", errors="replace") as fh:
                for idx, raw in enumerate(fh):
                    if idx < start:
                        continue
                    processed = idx + 1
                    new_lines.append(raw)
        except Exception:
            logger.exception("tail_rollout read failed for session %s", session_id[:8])
            return {"persisted": 0, "reason": "read_error"}

        _offsets[session_id] = processed
        # A first tail re-reads from line 0 (post-restart backstop): its
        # token_count events were already recorded pre-restart, so usage runs
        # in calibrate-only mode for that batch (see _process_rollout_lines).
        result = _process_rollout_lines(session_id, chat_id, new_lines, prefix=prefix,
                                        known_events=known_events,
                                        calibrate_usage=first_tail)
    result["from_line"] = start
    result["to_line"] = processed
    return result


def tail_lines(session_id: str, chat_id: str, lines: list[str]) -> dict:
    """Persist rollout lines FORWARDED from a remote satellite.

    The Codex twin of :func:`transcript_tailer.tail_lines`: the rollout JSONL lines
    arrive over the WS (``transcript_lines`` frame) from the satellite's
    ``CodexPtySession`` instead of a local file. The SATELLITE owns the once-only
    guarantee (a byte offset + a seek past pre-existing history on ``resume``), so
    there is no proxy-side line cursor / prefix-merge here — :func:`_process_rollout_lines`
    persists every message it is handed."""
    if not chat_id or not lines:
        return {"persisted": 0, "reason": "empty"}
    return _process_rollout_lines(session_id, chat_id, lines)


# Rollout tool names → the headless display names the dashboard already renders
# (the codex translator maps commandExecution→Bash / webSearch→web_search).
_PLAN_STATUSES = frozenset({"pending", "in_progress", "completed"})

_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", re.M)


def _plan_todos(args: dict) -> list[dict]:
    """``update_plan`` arguments → the TodoWrite ``todos`` shape the checklist
    panel rehydrates from (statuses normalized; unknown → pending)."""
    todos = []
    for step in args.get("plan") or []:
        if not isinstance(step, dict):
            continue
        raw = step.get("status", "")
        status = {"inProgress": "in_progress"}.get(raw, raw)
        todos.append({
            "content": step.get("step", ""),
            "status": status if status in _PLAN_STATUSES else "pending",
        })
    return todos


def _patch_summary(patch: str) -> str:
    """apply_patch input → changed-file summary, mirroring the headless
    fileChange summary (first 3 basenames + overflow count)."""
    names = [m.group(1).strip().replace("\\", "/").rsplit("/", 1)[-1]
             for m in _PATCH_FILE_RE.finditer(patch or "")]
    suffix = f" +{len(names) - 3}" if len(names) > 3 else ""
    return ", ".join(names[:3]) + suffix


# Multi-agent v2 tool surface (codex 0.144 — what an "ultra" turn calls to
# orchestrate its sub-agents). Rendered as regular tool cards with a
# task/message summary; the sub-agents themselves run on their own threads
# (their rollouts are thread_source=="subagent" — never tailed).
_MULTI_AGENT_TOOLS = frozenset({
    "spawn_agent", "wait_agent", "send_message",
    "interrupt_agent", "followup_task", "list_agents",
})

_CODE_MODE_EXEC_RE = re.compile(r"tools\.exec_command\(\s*(\{.*?\})\s*\)", re.S)


def _code_mode_bash(raw: str) -> str | None:
    """The single shell command inside a code-mode ``exec`` script, or None.

    Codex 0.144's code mode wraps shell commands as JS scripts calling
    ``tools.exec_command({...})`` under a custom tool named ``exec``. When the
    script is essentially ONE exec_command call, render it as the Bash card
    the headless translator emits for commandExecution items (display
    parity); a script composing multiple tool calls keeps the honest generic
    "exec" card with the script as its input."""
    if raw.count("tools.") != 1:
        return None
    m = _CODE_MODE_EXEC_RE.search(raw)
    if not m:
        return None
    try:
        args = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    cmd = args.get("cmd")
    return cmd if isinstance(cmd, str) and cmd else None


def _persist_block(task_store, chat_id: str, block: dict, known_events) -> int:
    """Persist one event block unless the first-tail backstop says it already
    is (post-restart re-read from line 0). Returns rows written (0/1)."""
    if known_events:
        if block["type"] == "thinking":
            key = "thinking:" + (block.get("content") or "")
        else:
            key = "tool:" + (block.get("tool_id") or "")
        if key in known_events:
            return 0
    persist_event(task_store, chat_id, block)
    return 1


def _on_rollout_tool_call(buf: ToolEventBuffer, task_store, chat_id: str,
                          payload: dict, known_events,
                          open_questions: set[str]) -> int:
    """One ``function_call`` / ``custom_tool_call`` item. Most open a pending
    ``tool`` block that persists when the matching ``*_output`` lands;
    ``update_plan`` persists a TodoWrite checklist snapshot immediately (its
    output is dropped, like the pump's synthesized block) and the delegation
    MCP is skipped entirely (its ``delegate_spawn`` row comes from the
    delegation endpoint).

    ``request_user_input`` (the AskUserQuestion analogue, exposed to the TUI
    via our ``default_mode_request_user_input`` feature flag) additionally
    registers in ``open_questions`` (batch state): the TUI blocks on its
    bottom-pane picker, so a call still unanswered at batch end folds the
    batch to a turn CLOSE (see ``_process_rollout_lines``) — mirroring the
    Claude tailer's ``AskUserQuestion`` handling."""
    name = payload.get("name", "")
    call_id = payload.get("call_id") or payload.get("id") or ""

    if payload.get("type") == "custom_tool_call":
        raw = payload.get("input") or ""
        args: dict = {"input": raw}
        if name == "apply_patch":
            summary = _patch_summary(raw)
        else:
            cmd = _code_mode_bash(raw) if name == "exec" else None
            if cmd:
                name = "Bash"
                summary = cmd[:100] + "..." if len(cmd) > 100 else cmd
            else:
                summary = raw[:100] + "..." if len(raw) > 100 else raw
    else:
        try:
            args = json.loads(payload.get("arguments") or "{}")
        except (json.JSONDecodeError, ValueError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        if name == "delegate":
            if call_id:
                buf.claim(call_id)
            return 0
        if name == "request_user_input":
            # Question card, persisted immediately — the answer output is
            # never attached (the model's next message reflects it), matching
            # the Claude question row. ``tool_id`` keys the post-restart
            # ``known_events`` dedupe (``tool:<id>``).
            if call_id:
                if not buf.claim(call_id):
                    return 0
                open_questions.add(call_id)
            return _persist_block(task_store, chat_id, {
                "type": "question", "tool_name": name,
                "tool_input": args, "tool_id": call_id,
            }, known_events)
        if name == "update_plan":
            if call_id and not buf.claim(call_id):
                return 0
            return _persist_block(task_store, chat_id, {
                "type": "tool", "name": "TodoWrite", "tool_id": call_id,
                "tool_input": {"todos": _plan_todos(args)},
            }, known_events)
        if name == "exec_command":
            cmd = str(args.get("cmd") or "")
            name = "Bash"
            summary = cmd[:100] + "..." if len(cmd) > 100 else cmd
        elif name in _MULTI_AGENT_TOOLS:
            # Multi-agent v2 (ultra / proactive orchestration): keep the wire
            # tool name on the card (honest — this is Codex's own machinery,
            # not the platform's delegate feature) and summarize with the
            # task/message the orchestrator provided (the generic key probe
            # below knows none of these arg names).
            summary = str(args.get("task_name") or args.get("message") or "")
            summary = summary[:100] + "..." if len(summary) > 100 else summary
        else:
            from core.layers.cli.helpers import _extract_tool_summary
            summary = _extract_tool_summary(name, args)

    block = {"type": "tool", "name": name, "tool_id": call_id or name,
             "summary": summary, "active": False, "tool_input": args}
    if not call_id:
        # No id to pair the output with — persist input-only rather than lose it.
        return _persist_block(task_store, chat_id, block, known_events)
    if buf.claim(call_id):
        buf.pending[call_id] = block
    return 0


def _on_rollout_tool_output(buf: ToolEventBuffer, task_store, chat_id: str,
                            call_id: str, output, known_events) -> int:
    """Pair a ``*_output`` item with its pending block and persist the pair."""
    blk = buf.close(call_id) if call_id else None
    if blk is None:
        return 0  # pre-buffer call, or a deliberately skipped tool's output
    if not isinstance(output, str):
        try:
            output = json.dumps(output)
        except (TypeError, ValueError):
            output = str(output)
    attach_result(blk, output)
    return _persist_block(task_store, chat_id, blk, known_events)


def _process_rollout_lines(session_id: str, chat_id: str, lines, *, prefix=None,
                           known_events=None, calibrate_usage: bool = False) -> dict:
    """Parse Codex rollout JSONL lines → persist user/assistant text + tool/
    thinking event rows to ``chat_messages``, capture the thread id into
    ``chats.codex_thread_id``, backfill the chat title, and report the per-turn
    ``task_complete`` signal.

    Shared by :func:`tail_rollout` (local file, cursor-sliced, prefix-merged on the
    first tail) and :func:`tail_lines` (remote forwarded, no prefix). ``prefix`` is
    the chronological ``(role, content)`` already in the DB that a resumed/restarted
    file tail must skip, and ``known_events`` its event-row twin (tool ids /
    thinking texts); the forwarded path passes ``None`` (the satellite already
    seeked past existing history). Persists EVERY non-prefix block it is handed
    — once-only is the caller's responsibility (the file line-cursor / the
    satellite's byte offset).

    Usage accounting: ``event_msg``/``token_count`` events carry
    ``info.last_token_usage`` (the API call's own tokens — OpenAI semantics:
    ``input_tokens`` INCLUDES ``cached_input_tokens``). The batch sums the
    events it newly consumed (claimed per line so overlapping deliveries can't
    double-count) and flushes one ``usage_records`` row via
    ``record_batch_usage`` — headless parity split: ``input_tokens`` column =
    non-cached input, ``cache_read`` = cached, ``cache_write`` = 0. The zero is
    forced by the CLI, not a shortcut: gpt-5.6+ bills cache writes (1.25x
    input) and the API reports them, but codex-rs 0.144.1's TokenUsage drops
    the field at deserialization — so no rollout event can ever carry it and
    5.6 costs undercount by the unreported write premium (see the translator's
    METADATA comment; re-check on pin bumps). ``calibrate_usage=True`` (a
    post-restart re-read from line 0) claims the events but records NOTHING —
    their usage was recorded pre-restart; recording the replay would
    double-count the whole session."""
    from storage import database as task_store
    prefix = prefix or []
    buf = _tool_events.setdefault(session_id, ToolEventBuffer())
    ptr = 0
    persisted = 0
    first_user_text: str | None = None
    # Turn-end signal for the interactive completion watcher: Codex writes one
    # ``event_msg``/``task_complete`` per turn (with the
    # final text in ``last_agent_message``). interactive_session gates the actual
    # completion on min-turn-time + fire-once, so this is just the signal.
    saw_task_complete = False
    last_agent_message = ""
    # The LAST turn-relevant record in the batch — a batch can straddle a turn
    # boundary ([task_complete, user new-prompt] in one forward), where
    # ``turn_complete`` alone would read "idle" while a new turn is opening.
    # Vocabulary shared with transcript_tailer: "user" (a prompt opened a
    # turn), "tool_use" (agent output mid-turn, no completion yet),
    # "end_turn" (task_complete), None (nothing turn-relevant).
    last_signal: str | None = None
    # request_user_input call_ids fired this batch and not yet answered. The
    # TUI blocks on the bottom-pane picker (no further items until the
    # function_call_output), so an id still open at batch end means the CLI
    # is parked on the question — the batch folds to a turn close below,
    # mirroring the Claude tailer's AskUserQuestion fold.
    open_questions: set[str] = set()
    # compacted rollout item seen this batch (see the handler below).
    compacted = False
    # Provider usage newly consumed this batch (single accumulator — the model
    # rides _usage_models, keyed per turn_context). Flushed once at batch end.
    usage_acc = {"input_tokens": 0, "output_tokens": 0,
                 "cache_read": 0, "cache_write": 0}
    for raw in lines:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        otype = obj.get("type")
        payload = obj.get("payload") or {}

        if otype == "session_meta":
            tid = payload.get("id") or ""
            if tid and session_id not in _thread_saved:
                try:
                    task_store.update_chat(chat_id, codex_thread_id=tid)
                except Exception:
                    logger.exception(
                        "codex tail %s: thread-id persist failed", session_id[:8]
                    )
                _thread_saved.add(session_id)
            continue

        if otype == "turn_context":
            # Names the model serving this turn — the usage rows' model field
            # (token_count events don't carry one).
            if payload.get("model"):
                _usage_models[session_id] = str(payload["model"])
            continue

        if otype == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            total = info.get("total_token_usage") or {}
            # Claim per event line — no id, so key on the envelope timestamp +
            # the cumulative total (monotonic per session): unique per real
            # event, identical on a re-delivery.
            key = (f"usage:{obj.get('timestamp', '')}"
                   f":{total.get('total_tokens', 0)}")
            if last and buf.claim(key) and not calibrate_usage:
                inp = int(last.get("input_tokens") or 0)
                cached = int(last.get("cached_input_tokens") or 0)
                usage_acc["input_tokens"] += max(0, inp - cached)
                usage_acc["cache_read"] += cached
                usage_acc["output_tokens"] += int(last.get("output_tokens") or 0)
            continue

        if otype == "event_msg" and payload.get("type") in ("error", "stream_error"):
            # CLI-reported stream/API error (its own marker — never model
            # prose): a provider limit rests the account and fails the scope
            # over now. Claude twin: transcript_tailer's isApiErrorMessage hook.
            try:
                from services.engines.subscription_pool import throttle_from_cli_error
                throttle_from_cli_error(
                    session_id, str(payload.get("message") or ""))
            except Exception:
                logger.exception(
                    "codex tail %s: limit-detection failed", session_id[:8])
            continue

        if otype == "event_msg" and payload.get("type") == "task_complete":
            # Per-turn completion marker — the agent finished this turn.
            saw_task_complete = True
            last_signal = "end_turn"
            lam = payload.get("last_agent_message")
            if lam:
                last_agent_message = lam
            continue

        if otype == "event_msg" and payload.get("type") == "turn_aborted":
            # ESC / dashboard Stop: the turn ended by interruption — no
            # task_complete follows. Close the turn WITHOUT the completion
            # signal (the user stopped it themselves; no finished ping).
            last_signal = "end_turn"
            continue

        if otype == "compacted":
            # Context compaction finished (codex-rs RolloutItem::Compacted).
            # Claude twin lives in transcript_tailer (compact_boundary):
            # surface a history row + flag the batch so an IDLE session runs
            # turn-end effects; a mid-turn auto-compact keeps its open turn.
            persist_event(task_store, chat_id, {
                "type": "system",
                "subtype": "context_compressed",
                "message": "Conversation compacted",
            })
            persisted += 1
            compacted = True
            continue

        if otype != "response_item":
            continue
        ptype = payload.get("type")
        if ptype in ("function_call", "custom_tool_call"):
            persisted += _on_rollout_tool_call(buf, task_store, chat_id,
                                               payload, known_events,
                                               open_questions)
            if payload.get("name") == "request_user_input":
                # Turn-relevant: codex tool calls otherwise don't move the
                # signal, but the question fold below keys on "tool_use"
                # (Claude parity — its question tool_use sets the same).
                last_signal = "tool_use"
            continue
        if ptype in ("function_call_output", "custom_tool_call_output"):
            open_questions.discard(payload.get("call_id") or "")
            persisted += _on_rollout_tool_output(
                buf, task_store, chat_id, payload.get("call_id") or "",
                payload.get("output"), known_events)
            continue
        if ptype == "tool_search_call":
            args = payload.get("arguments") \
                if isinstance(payload.get("arguments"), dict) else {}
            call_id = payload.get("call_id") or payload.get("id") or ""
            if call_id and buf.claim(call_id):
                buf.pending[call_id] = {
                    "type": "tool", "name": "ToolSearch", "tool_id": call_id,
                    "summary": args.get("query", ""), "active": False,
                    "tool_input": args,
                }
            continue
        if ptype == "tool_search_output":
            persisted += _on_rollout_tool_output(
                buf, task_store, chat_id, payload.get("call_id") or "",
                json.dumps(payload.get("tools") or []), known_events)
            continue
        if ptype == "web_search_call":
            # Server-side tool — no output item pairs with it; the results ride
            # the model's context. One completed row at the call's position.
            wid = payload.get("call_id") or payload.get("id") or ""
            action = payload.get("action")
            query = (action.get("query") or "") if isinstance(action, dict) else ""
            if wid and not buf.claim(wid):
                continue
            persisted += _persist_block(task_store, chat_id, {
                "type": "tool", "name": "web_search", "tool_id": wid or "web_search",
                "summary": query, "active": False,
                "tool_input": {"query": query} if query else None,
            }, known_events)
            continue
        if ptype == "reasoning":
            # Headless parity: the summary text is what streams as THINKING
            # (raw chains arrive encrypted — nothing readable to persist then).
            texts = [s.get("text", "") for s in payload.get("summary") or []
                     if isinstance(s, dict)]
            texts += [c.get("text", "") for c in payload.get("content") or []
                      if isinstance(c, dict)
                      and c.get("type") in ("reasoning_text", "text")]
            content_text = "\n\n".join(t for t in texts if t and t.strip())
            if not content_text.strip():
                continue
            rid = payload.get("id") or ""
            if rid and not buf.claim(rid):
                continue
            persisted += _persist_block(task_store, chat_id, {
                "type": "thinking", "content": content_text,
            }, known_events)
            continue
        if ptype != "message":
            continue
        role = payload.get("role")
        if role not in ("user", "assistant"):
            continue  # developer (perms) + any other synthetic role
        text = _extract_text(payload.get("content"))
        if role == "user":
            # Drop any restored-history digest the reseed prepended to the
            # cold prompt — only the real prompt belongs in chat_messages.
            text = strip_seed_prefix(text)
        if not text.strip():
            continue
        if role == "user" and _is_synthetic_user(text):
            continue  # AGENTS.md / environment-context injection

        if role == "user" and first_user_text is None:
            first_user_text = text

        # Turn-relevant either way — a prefix-skipped record is still the
        # batch's latest turn state (a resume's first tail re-reads history).
        last_signal = "user" if role == "user" else "tool_use"

        # Prefix-merge: already persisted (resume/restart) → consume + skip.
        if ptr < len(prefix) and prefix[ptr] == (role, text):
            ptr += 1
            continue

        # The dashboard warmup already persisted this exact prompt at
        # send-time — skip the row, keep the turn signal/title (twin of the
        # Claude tailer's consume; see transcript_tool_events).
        if role == "user" and consume_sent_prompt(chat_id, text):
            continue

        task_store.add_chat_message(chat_id, role, text)
        persisted += 1

    # Question fold: a request_user_input still unanswered at batch end means
    # the TUI is blocked on the picker — close the turn (headless parity: the
    # pump's turn would end right after the question card) so the live dot
    # clears and the "needs your input" ping can fire. Never overrides a REAL
    # later signal: "user" (new prompt) and "end_turn" (turn_aborted from ESC
    # / real completion) win — those paths must not read as question-parked.
    question_pending = bool(open_questions) and last_signal in ("tool_use", None)
    if question_pending:
        last_signal = "end_turn"

    title_set = _maybe_backfill_title(session_id, chat_id, first_user_text)

    usage_rows = 0
    if any(usage_acc.values()) and chat_id:
        usage_rows = record_batch_usage(
            session_id, chat_id,
            {_usage_models.get(session_id, ""): usage_acc}, task_store)

    if persisted:
        logger.info(
            "interactive codex %s: persisted %d rollout message(s) to chat %s%s",
            session_id[:8], persisted, chat_id, " (titled)" if title_set else "",
        )
    return {
        "persisted": persisted,
        "title_set": title_set,
        # Interactive completion signal (consumed by interactive_session).
        "turn_complete": saw_task_complete,
        "last_message": last_agent_message,
        "last_signal": last_signal,
        # Turn parked on an unanswered request_user_input (folded to end_turn
        # above) — interactive_session rewords the ping + gates injection.
        "question_pending": question_pending,
        # A compacted rollout item landed in this batch (see handler above).
        "compacted": compacted,
        # usage_records rows written for this batch (0 = no new usage events).
        "usage_rows": usage_rows,
    }


def _maybe_backfill_title(session_id: str, chat_id: str, first_user_text: str | None) -> bool:
    """Backfill the chat title from the first user prompt — interactive chats can't
    title at send-time (the prompt rides argv/PTY). Only when still untitled, so we
    never overwrite an existing title and it's idempotent across tails."""
    if not first_user_text or not chat_id:
        return False
    from storage import database as task_store
    try:
        rec = task_store.get_chat(chat_id)
        if rec and not (rec.get("title") or "").strip():
            new_title = _title_from_prompt(first_user_text)
            if not new_title:
                return False
            task_store.update_chat(chat_id, title=new_title)
            try:
                from services.notifications import notification_manager
                notification_manager.broadcast_chat_title(
                    rec.get("user_sub", ""), chat_id, new_title,
                    agent=rec.get("agent") or "",
                )
            except Exception:
                pass
            return True
    except Exception:
        logger.exception("codex tail title backfill failed for %s", session_id[:8])
    return False
