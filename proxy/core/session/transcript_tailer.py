"""Persist an interactive CLI session's transcript JSONL into chat_messages.

Interactive sessions don't flow through the pump, so nothing writes their turns
to the DB. On the ``Stop`` hook the proxy reads the
Claude transcript JSONL (``transcript_path`` forwarded by ``hooks/stop_tracker.py``)
and appends new **user/assistant text, thinking and tool events** to
``chat_messages`` for dashboard visibility. (Resume itself uses the CLI's own
``--resume`` off the same JSONL, so it does not depend on this.)

Tool events persist as the SAME ``role="event"`` rows the headless pump writes
(``stream_pump._save_turn_blocks``) so the dashboard's existing cards re-render
an interactive chat with no frontend changes: ``tool_use``/``tool_result``
pairs → one ``tool`` block (input summary via the CLI layer's
``_extract_tool_summary``; result truncated + summarized per the PostToolUse
hook's policy — see ``transcript_tool_events``), ``thinking`` blocks →
``thinking`` rows, ``Task``/``Agent`` spawns → ``task_spawn`` rows (fg result
attached), ``EnterPlanMode``/``ExitPlanMode`` → ``plan_mode`` rows,
``AskUserQuestion`` → ``question`` rows (and, while unanswered, a turn-close
fold — see ``_process_lines``), and the delegation MCP's ``delegate``
is skipped (the delegation endpoint persists its own ``delegate_spawn`` row).
A ``tool_use`` and its ``tool_result`` ride separate transcript lines — often
separate tail batches — so blocks wait in a per-session ``ToolEventBuffer`` and
persist when the result lands (transcript order is preserved: a result always
precedes the turn's next assistant message). A pending block whose result never
arrives (abort/kill) is dropped, matching the pump. Slash-command noise is
filtered: ``isMeta`` lines, ``<command-name>``/``<local-command-stdout>``
wrapper messages and raw ``## Context Usage`` reports never persist as user
text (the dashboard's ``transcriptCleanup.ts`` hides pre-existing rows).

It also feeds the per-session ``SubagentRegistry`` straight from the transcript —
a ``Task``/``Agent`` ``tool_use`` → ``register_spawn``; the matching
``tool_result`` → ``mark_done`` — because interactive emits no stream-json
``task_started``. Interactive never runs the headless turn loop, so
``reset_subagent_registry`` is never called under it; the registry simply
accumulates from the transcript.

The tailer is also the ONLY writer of ``usage_records`` for interactive
sessions (the pump records headless turns; interactive has no pump): every
assistant transcript line carries ``message.usage`` + ``message.model``, so
each fold batch accumulates the newly consumed tokens and writes one usage row
per model via ``transcript_tool_events.record_batch_usage`` — attributed to
the serving subscription (``source_key``) so the pool's least-consumed routing
finally sees interactive burn. Dedupe rule: one API message = one line PER
CONTENT BLOCK, same ``message.id``, identical usage repeated on each — so
usage is claimed once per message id.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime

from core.session.history_seed import strip_seed_prefix
from core.session.session_state import get_subagent_registry
from core.session.transcript_tool_events import (
    TailLocks, ToolEventBuffer, attach_result, consume_sent_prompt,
    extract_result_text, persist_event, record_batch_usage, truncate_result,
)

logger = logging.getLogger("claude-proxy.transcript_tailer")

# session_id → number of transcript lines already processed. The Claude JSONL is
# append-only within a session, so a line offset is a sufficient, cheap cursor.
_offsets: dict[str, int] = {}
# session_id → wall-clock at attach (set by seek_past_existing). The replay
# guard for RESUME REWARMS: `claude --resume <old> --session-id <new>` writes a
# NEW transcript file that REPLAYS the prior conversation as copied lines — the
# attach-time seek ran before that file existed (cursor 0), so without this the
# first tail re-persisted the whole history as duplicates (text, tool rows, and
# usage — live-observed as a phantom multi-dollar usage row per re-warm).
# Copied lines keep their ORIGINAL timestamps, so any line stamped before the
# attach is a replay. Local-path only: satellite-forwarded lines (tail_lines)
# ride the satellite's own seek and a remote clock this bound must not judge.
_attach_ts: dict[str, float] = {}
# Allowance for CLI-vs-proxy stamp jitter (same machine; a copied line is
# stamped in the PREVIOUS session, strictly before this attach).
_ATTACH_TS_SKEW_S = 1.0
# session_id → tool_use/tool_result pairing + dedupe state (see ToolEventBuffer).
# In-memory like the cursor: the local path always seeks before it tails, and the
# satellite path owns once-only delivery, so neither re-reads persisted lines.
_tool_events: dict[str, ToolEventBuffer] = {}
# Serializes cursor+read+persist per session — overlapping tail triggers (debounce
# / sweep / close / Stop hook) run in separate threads and would otherwise both
# read the cursor before either advances it, persisting the same slice twice.
_tail_locks = TailLocks()


def forget(session_id: str) -> None:
    """Drop the cursor + pairing state (called on interactive-session close).
    Still-pending tool blocks are dropped with it — an aborted/killed call has
    no result and the pump doesn't persist those headless either."""
    _offsets.pop(session_id, None)
    _tool_events.pop(session_id, None)
    _attach_ts.pop(session_id, None)
    _tail_locks.forget(session_id)


def seek_past_existing(session_id: str, chat_id: str = "") -> int:
    """Initialize the tail cursor at the transcript's CURRENT end.

    Called at interactive ATTACH, before the CLI starts. Resuming an existing
    chat interactively reuses the same on-disk transcript the headless turns
    already wrote (their user lines even carry the injected ``[Current time:]``
    header), and a re-toggle reuses the previous stint's file — so everything
    in the file at attach time is already persisted (by the pump or by earlier
    tails). Without this seek the first tail reads from line 0 and re-inserts
    the whole prior conversation into ``chat_messages`` as duplicates. Mirrors
    the satellite forwarder, which seeks past pre-existing history on its side.
    ``chat_id`` is unused — signature parity with ``codex_rollout_tailer`` so
    ``interactive_session`` can call either tailer flavor-agnostically.
    """
    path = resolve_transcript_path(session_id)
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
    # Arm the resume-replay guard (see _attach_ts): lines stamped before this
    # moment are copies of an earlier session's history, not new turn content.
    _attach_ts[session_id] = time.time()
    if n:
        logger.info(
            "interactive %s: transcript seek past %d pre-existing line(s)",
            session_id[:8], n,
        )
    return n


def resolve_transcript_path(session_id: str) -> str | None:
    """Find the Claude session JSONL on disk WITHOUT the Stop hook.

    The CLI writes ``<claude_dir>/projects/<project-hash>/<session_id>.jsonl``;
    the project hash derives from the (sandbox) CWD, so search every project dir
    under the session's host ``.claude``. This is the robust path — the native
    TUI does not reliably fire the ``Stop`` hook, so we discover + tail the
    transcript ourselves on close + periodically rather than depend on it."""
    from core.session.session_state import get_session_claude_dir
    claude_dir = get_session_claude_dir(session_id)
    if not claude_dir:
        return None
    projects = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects):
        return None
    try:
        for proj in os.listdir(projects):
            candidate = os.path.join(projects, proj, f"{session_id}.jsonl")
            if os.path.isfile(candidate):
                return candidate
    except OSError:
        return None
    return None


def resolve_and_tail(session_id: str, chat_id: str) -> dict:
    """Discover the transcript path (no Stop-hook dependency) and tail it.

    Called on interactive-session close (so a killed/reaped/toggled-off chat
    persists its conversation to ``chat_messages``) and periodically by the idle
    reaper sweep (so a proxy crash can't lose the turns)."""
    path = resolve_transcript_path(session_id)
    if not path:
        return {"persisted": 0, "reason": "no_transcript"}
    return tail_transcript(session_id, chat_id, path)


# Injected `[Current time: ...]` stamp line(s) — the platform prepends them to
# interactive prompts and the PTY transcript persists them verbatim. Twin of the
# dashboard's transcriptCleanup.ts matcher: start-anchored exact shape only.
_TIME_PRELUDE_RE = re.compile(r"^\[Current time: [^\]\n]{1,160}\][ \t]*(?:\r?\n+|$)")

# Twin of ``ws/dashboard_chat._APP_ACTION_HEADER_RE`` — see there.
_APP_ACTION_HEADER_RE = re.compile(
    r'^\[action from mini-app "(.{1,200}?)" — (.{1,80}?)\]'
)


def _title_from_prompt(text: str) -> str:
    """Deterministic chat title from the first user message — mirrors
    ``ws/dashboard.py:_deterministic_title`` (first ~6 words / 48 chars,
    whitespace-collapsed, ellipsis if truncated). Interactive chats can't title
    at send-time (the prompt rides the PTY, not the pump's ``_persist_first_prompt``),
    so the tailer backfills it from the transcript on the first read. Injected
    ``[Current time: ...]`` stamps are dropped first; a prompt with nothing
    user-authored left returns "" so the chat stays untitled for the next real
    prompt (callers skip the empty title)."""
    out = text or ""
    while True:
        stripped = _TIME_PRELUDE_RE.sub("", out, count=1)
        if stripped == out:
            break
        out = stripped
    # Mini-app action framing (typed into interactive terminals by the
    # dashboard) titles as "App — Label" — twin of the recognizer in
    # ``ws/dashboard_chat._APP_ACTION_HEADER_RE``.
    m = _APP_ACTION_HEADER_RE.match(out)
    if m:
        out = f"{m.group(1)} — {m.group(2)}"
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


def _extract_text(content) -> str:
    """Concatenate the text of a message's content (str or block list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _tool_result_blocks(content) -> list[dict]:
    """The tool_result blocks of a user message's content (empty for prompts)."""
    if not isinstance(content, list):
        return []
    return [
        b for b in content
        if isinstance(b, dict) and b.get("type") == "tool_result"
    ]


# Slash-command noise: Claude Code writes local command invocations and their
# output into the transcript as user messages wrapped in these tags, and
# ``/context`` reports as a raw ``## Context Usage`` markdown dump. Twin of the
# dashboard's transcriptCleanup.ts matcher (which hides pre-existing rows
# render-side): recognized only when the message STARTS with a wrapper tag or
# is a structural context report — pasted markdown mid-message never matches.
_COMMAND_TAG_RE = re.compile(
    r"<(command-name|command-message|command-args|local-command-stdout)>"
    r"[\s\S]*?</\1>"
)
_COMMAND_TAG_START_RE = re.compile(
    r"^<(command-name|command-message|command-args|local-command-stdout)>"
)


def _strip_command_noise(text: str) -> str | None:
    """Filter slash-command noise from a user message before it persists.

    Returns the text to persist, or ``None`` when nothing user-authored
    remains (pure command record / context report) and the row is skipped."""
    trimmed = (text or "").strip()
    if _COMMAND_TAG_START_RE.match(trimmed):
        remainder = _COMMAND_TAG_RE.sub("", trimmed).strip()
        return remainder or None
    if trimmed.startswith("## Context Usage") and "**Tokens:**" in trimmed:
        return None
    return text


# Harness-injected user-role lines: background bash / subagent completions
# arrive in the transcript as user messages wrapping a <task-notification>
# block. Real typed prompts carry origin.kind == "human"; plain lines from
# some entrypoints carry no origin at all — hence a DENY-list on kind (an
# allow-list would drop legit origin-less prompts).
_SYNTHETIC_ORIGIN_KINDS = frozenset({"task-notification"})


def _is_harness_injected_user_line(obj: dict, text: str) -> bool:
    """True for a user row the harness injected (never typed by the user).

    Primary discriminator: the top-level ``origin.kind`` stamp. The content
    match is defense in depth for entrypoints that omit ``origin`` — the
    message must START with the tag AND carry the closing tag, so a user
    pasting a fragment to discuss it is never dropped (a full verbatim paste
    as the entire message is the accepted rare cost, same trade-off as the
    command-noise filter)."""
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind") in _SYNTHETIC_ORIGIN_KINDS:
        return True
    stripped = (text or "").lstrip()
    return (
        stripped.startswith("<task-notification>")
        and "</task-notification>" in stripped
    )


def _on_tool_use(buf: ToolEventBuffer, reg, task_store, chat_id: str,
                 block: dict, open_questions: set[str]) -> int:
    """Dispatch one assistant ``tool_use`` block; returns rows persisted now.

    Mirrors the pump's block mapping (``_SKIP_TOOL_PERSIST`` + dedicated
    events): Task/Agent → pending ``task_spawn``; plan-mode + question tools →
    immediate rows (their results are never attached headless); the delegation
    MCP is skipped (the delegation endpoint writes its own ``delegate_spawn``
    row); everything else → a pending ``tool`` block awaiting its result.

    ``AskUserQuestion`` additionally registers in ``open_questions`` (batch
    state): the harness blocks the turn on it, so an id still unanswered at
    batch end folds the batch to a turn CLOSE (see ``_process_lines``)."""
    name = block.get("name", "")
    tuid = block.get("id", "")
    tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}

    if name == "mcp__delegation-mcp__delegate":
        if tuid:
            buf.claim(tuid)
        return 0

    if name in ("Task", "Agent"):
        # Subagent spawn — gate turn-end on its completion (the tool_result,
        # seen on a later batch, marks done) + persist the pump-shaped
        # task_spawn row once that result lands (fg report attached there).
        if not tuid:
            return 0
        reg.register_spawn(tuid, tuid)
        buf.open(tuid, {
            "type": "task_spawn",
            "description": tool_input.get("description", "?"),
            "subagent_type": tool_input.get("subagent_type", ""),
            "run_in_background": bool(tool_input.get("run_in_background", False)),
            "tool_use_id": tuid,
            "tool_input": tool_input,
        })
        return 0

    if tuid and not buf.claim(tuid):
        return 0  # duplicate id (overlapping tails) — already handled

    if name in ("EnterPlanMode", "ExitPlanMode"):
        evt: dict = {"type": "plan_mode",
                     "action": "enter" if name == "EnterPlanMode" else "exit"}
        if name == "ExitPlanMode":
            evt["tool_input"] = tool_input
        persist_event(task_store, chat_id, evt)
        return 1

    if name == "AskUserQuestion":
        if tuid:
            open_questions.add(tuid)
        persist_event(task_store, chat_id, {
            "type": "question", "tool_name": name, "tool_input": tool_input,
        })
        return 1

    from core.layers.cli.helpers import _extract_tool_summary
    tool_block = {
        "type": "tool", "name": name, "tool_id": tuid or name,
        "summary": _extract_tool_summary(name, tool_input),
        "active": False, "tool_input": tool_input,
    }
    if not tuid:
        # No id to pair the result with — persist input-only rather than lose it.
        persist_event(task_store, chat_id, tool_block)
        return 1
    buf.pending[tuid] = tool_block
    return 0


def tail_transcript(session_id: str, chat_id: str, transcript_path: str) -> dict:
    """Process transcript lines added since the last tail (LOCAL file path).

    Reads the on-disk JSONL from the per-session line cursor and hands the new
    lines to :func:`_process_lines`. Idempotent across calls via the cursor.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return {"persisted": 0, "reason": "no_file"}

    with _tail_locks.acquire(session_id):
        start = _offsets.get(session_id, 0)
        processed = start
        new_lines: list[str] = []
        try:
            with open(transcript_path, "r", encoding="utf-8", errors="replace") as fh:
                for idx, raw in enumerate(fh):
                    if idx < start:
                        continue
                    processed = idx + 1
                    new_lines.append(raw)
        except Exception:
            logger.exception("tail_transcript read failed for session %s", session_id[:8])
            return {"persisted": 0, "reason": "read_error"}

        _offsets[session_id] = processed
        result = _process_lines(session_id, chat_id, new_lines,
                                min_line_ts=_attach_ts.get(session_id))
    result["from_line"] = start
    result["to_line"] = processed
    return result


def tail_lines(session_id: str, chat_id: str, lines: list[str]) -> dict:
    """Persist transcript lines FORWARDED from a remote satellite.

    Same parse/persist as the local file tailer, but the JSONL lines arrive over
    the WS (``transcript_lines`` frame) instead of a local file the proxy can
    read. The SATELLITE owns the once-only guarantee (it forwards each line once,
    tracked by a byte offset, and seeks past pre-existing history on ``--resume``),
    so there is no proxy-side line cursor here — :func:`_process_lines` persists
    every text block it is handed."""
    if not chat_id or not lines:
        return {"persisted": 0, "reason": "empty"}
    return _process_lines(session_id, chat_id, lines)


def _process_lines(session_id: str, chat_id: str, lines, *,
                   min_line_ts: float | None = None) -> dict:
    """Parse transcript JSONL lines → persist user/assistant text to
    ``chat_messages``, drive the SubagentRegistry from Task spawns/results,
    backfill the chat title, and report the turn-end signal.

    Shared by :func:`tail_transcript` (local file, cursor-sliced) and
    :func:`tail_lines` (remote forwarded). Persists EVERY text block in ``lines``
    — once-only is the caller's responsibility (the file line-cursor or the
    satellite's byte offset) — EXCEPT lines stamped before ``min_line_ts``
    (the attach time): a resume re-warm's new transcript REPLAYS the prior
    conversation as copied lines carrying their original timestamps, and the
    attach-time seek can't skip a file that didn't exist yet (see
    ``_attach_ts``). Only the local file path passes the bound; the satellite
    path passes ``None`` (its own seek owns replay exclusion, and a remote
    clock must not be judged against proxy wall-time)."""
    from storage import database as task_store  # late import: DB hub
    reg = get_subagent_registry(session_id)
    buf = _tool_events.setdefault(session_id, ToolEventBuffer())

    persisted = 0
    first_user_text: str | None = None  # for the chat-title backfill (below)
    # Turn-end signal for the interactive completion watcher: an assistant
    # message with stop_reason=="end_turn" is the turn
    # boundary (vs "tool_use" = more tools coming). Track the LAST assistant
    # message seen this batch; interactive_session gates the actual completion on
    # bg-registry-empty + min-turn-time + fire-once, so this is just the signal.
    last_stop_reason: str | None = None
    last_assistant_text = ""
    # The LAST turn-relevant record in the batch — a batch can straddle a turn
    # boundary (e.g. [assistant end_turn, user new-prompt] within one debounce
    # window), where ``turn_complete`` alone would read "idle" while a new turn
    # is actually opening. Consumers deriving turn-open state must use THIS,
    # not ``turn_complete``. Values: "user" (a prompt opened a turn),
    # "tool_use" (mid-turn), "end_turn" (turn closed), None (nothing
    # turn-relevant in the batch — tool_result-only lines don't count).
    last_signal: str | None = None
    # AskUserQuestion ids asked this batch and not yet answered. The harness
    # BLOCKS the turn on the question (no further assistant lines until the
    # tool_result), so an id still open at batch end means the CLI is parked
    # on the question dialog — the batch folds to a turn close below, the
    # interactive twin of headless's turn ending after the question card.
    open_questions: set[str] = set()
    # compact_boundary seen this batch (see the system-line handler below).
    # compact_trigger carries compactMetadata.trigger ("manual" | "auto") so
    # interactive_session can close a stale-open turn on a MANUAL /compact —
    # manual compaction only completes with the CLI idle at the prompt.
    compacted = False
    compact_trigger = ""
    # Provider usage newly consumed this batch, keyed by model (a session can
    # switch models mid-life via /model). Flushed once at batch end — see
    # transcript_tool_events.record_batch_usage.
    usage_by_model: dict[str, dict[str, int]] = {}
    for raw in lines:
        raw = (raw or "").strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue

        kind = obj.get("type")
        if obj.get("isSidechain"):
            # Subagent-internal lines. Current CLIs write them into separate
            # subagents/*.jsonl files this tailer never opens; the guard keeps
            # a format regression from bleeding subagent turns into the chat.
            continue
        if obj.get("isCompactSummary") or obj.get("isVisibleInTranscriptOnly"):
            # The post-compaction reseed ("This session is being continued…"):
            # a SYNTHETIC user line the CLI writes after compacting. Persisting
            # it dumped the whole summary into chat history as a fake prompt
            # AND folded last_signal="user" — a phantom turn nothing closes
            # (the "still working forever after /compact" symptom).
            continue
        if kind == "system" and obj.get("subtype") == "compact_boundary":
            # Compaction finished. Surface it in the rich history and flag the
            # batch so interactive_session can run turn-end effects when the
            # session is idle (manual /compact) — an auto-compact mid-turn
            # keeps its open turn and the real end_turn closes it normally.
            meta = obj.get("compactMetadata") or {}
            trigger = meta.get("trigger") or ""
            pre = meta.get("preTokens")
            post = meta.get("postTokens")
            detail = ""
            if isinstance(pre, int) and isinstance(post, int) and pre > post:
                detail = f" ({pre - post:,} tokens freed)"
            # Same subtype the live headless compact path appends — SystemEvent
            # renders the amber "Context compressed" separator for both.
            persist_event(task_store, chat_id, {
                "type": "system",
                "subtype": "context_compressed",
                "message": f"Conversation compacted{detail}",
                "trigger": trigger,
            })
            persisted += 1
            compacted = True
            compact_trigger = trigger
            continue
        if min_line_ts is not None:
            # Resume-replay guard (see docstring): a line stamped before the
            # attach is a COPY of an earlier session's history — already
            # persisted (and usage-recorded) when it originally happened.
            # Lines without a timestamp (mode/meta records) pass through; real
            # turn-content lines always carry one.
            ts_raw = obj.get("timestamp")
            if isinstance(ts_raw, str) and ts_raw:
                try:
                    line_ts = datetime.fromisoformat(
                        ts_raw.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    line_ts = None
                if line_ts is not None and \
                        line_ts < min_line_ts - _ATTACH_TS_SKEW_S:
                    continue
        msg = obj.get("message") or {}
        content = msg.get("content")

        if obj.get("isApiErrorMessage"):
            # A CLI-synthesized API-error row (its own marker — never model
            # prose, which routinely discusses rate limits): if it names a
            # provider limit, rest the account and fail the scope over now.
            # Placed after the replay guard so a re-attach replay of an old
            # error can't re-mark. The row still persists as assistant text
            # below — the user should see the error in the chat.
            try:
                from services.engines.subscription_pool import throttle_from_cli_error
                throttle_from_cli_error(session_id, _extract_text(content))
            except Exception:
                logger.exception(
                    "tailer limit-detection failed for %s", session_id[:8])

        if kind == "user":
            results = _tool_result_blocks(content)
            if results:
                # Tool outputs, not user input — drive the registry and pair
                # each result with its pending block (persisted here, at the
                # result's transcript position).
                for rb in results:
                    tuid = rb.get("tool_use_id", "")
                    if not tuid:
                        continue
                    open_questions.discard(tuid)  # question answered in-batch
                    reg.mark_done(tuid, buffer=False)
                    blk = buf.close(tuid)
                    if blk is None:
                        continue  # pre-buffer call or deliberately skipped
                    text = extract_result_text(rb.get("content"))
                    if blk["type"] == "task_spawn":
                        # A denied spawn (permission-rejected — the result is
                        # the error) never started; headless persists no row
                        # for it (task_started never fires), so neither do we.
                        if rb.get("is_error"):
                            continue
                        # fg subagent report — bg results are just the
                        # "launched" ack, which the pump skips too.
                        if not blk.get("run_in_background") and text:
                            blk["tool_result"] = truncate_result(text)
                    else:
                        attach_result(blk, text,
                                      is_error=bool(rb.get("is_error")))
                    persist_event(task_store, chat_id, blk)
                    persisted += 1
                continue
            if obj.get("isMeta"):
                continue  # local-command caveat / CLI meta — never user text
            # Drop any restored-history digest the reseed prepended to the
            # cold prompt — only the real prompt belongs in chat_messages.
            text = strip_seed_prefix(_extract_text(content))
            if _is_harness_injected_user_line(obj, text):
                continue  # bg task-notification — harness, not the user
            if text.strip().startswith("[Request interrupted by user"):
                # ESC / dashboard Stop landed mid-turn: the CLI writes this
                # marker instead of a result event ("… by user]" text-phase,
                # "… by user for tool use]" tool-phase; content is a plain
                # string or one text block depending on entrypoint). It is
                # turn STATE, not a prompt — close the turn and keep the
                # marker out of chat history. Pending fg spawns died with
                # the turn (mirror the new-prompt un-pend below).
                for tuid, blk in list(buf.pending.items()):
                    if (blk.get("type") == "task_spawn"
                            and not blk.get("run_in_background")):
                        buf.pending.pop(tuid, None)
                        reg.mark_done(tuid, buffer=False)
                last_signal = "end_turn"
                continue
            if text.strip():
                cleaned = _strip_command_noise(text)
                if cleaned is None:
                    continue  # slash-command record — local output, not a prompt
                # A fg subagent can't span a user turn — a new REAL prompt
                # means any still-pending fg spawn was interrupted without a
                # result line. Un-pend it (drop the block, complete the
                # registry entry) so the bg_pending gates can't wedge the
                # session; the pump doesn't persist aborted spawns either.
                for tuid, blk in list(buf.pending.items()):
                    if (blk.get("type") == "task_spawn"
                            and not blk.get("run_in_background")):
                        buf.pending.pop(tuid, None)
                        reg.mark_done(tuid, buffer=False)
                last_signal = "user"
                if first_user_text is None:
                    first_user_text = cleaned
                if consume_sent_prompt(chat_id, cleaned):
                    # The dashboard warmup already persisted this exact prompt
                    # at send-time — skip the row, keep the turn signal/title.
                    continue
                task_store.add_chat_message(chat_id, "user", cleaned)
                persisted += 1

        elif kind == "assistant" and isinstance(content, list):
            # Usage accounting: the CLI writes one transcript line PER CONTENT
            # BLOCK of an API message, each repeating the SAME message.id with
            # IDENTICAL usage (verified on real transcripts) — so claim by
            # message id and count once, whether the blocks land in one batch
            # or straddle two. Synthetic lines (API-error rows) carry no id
            # and are skipped.
            usage = msg.get("usage")
            msg_id = msg.get("id")
            if (isinstance(usage, dict) and msg_id
                    and buf.claim(f"usage:{msg_id}")):
                acc = usage_by_model.setdefault(msg.get("model") or "", {
                    "input_tokens": 0, "output_tokens": 0,
                    "cache_read": 0, "cache_write": 0,
                })
                acc["input_tokens"] += int(usage.get("input_tokens") or 0)
                acc["output_tokens"] += int(usage.get("output_tokens") or 0)
                acc["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                acc["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
            msg_text_parts: list[str] = []
            for bi, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    text = block.get("text", "")
                    if text.strip():
                        # Claimed by line uuid + block index, exactly like
                        # thinking below: the CLI can re-write a line and a
                        # concurrent-tail race can re-deliver one — text was
                        # the only unclaimed block type, so the SAME assistant
                        # text persisted twice (live-hit 2026-07-19: 37
                        # consecutive duplicate rows in one interactive chat;
                        # tool/thinking/usage were already claim-protected).
                        luid = obj.get("uuid") or ""
                        if luid and not buf.claim(f"text:{luid}:{bi}"):
                            continue
                        task_store.add_chat_message(chat_id, "assistant", text)
                        persisted += 1
                        msg_text_parts.append(text)
                elif btype == "thinking":
                    # Headless parity: the pump persists a thinking block per
                    # phase with its text; adaptive-effort models emit empty
                    # thinking (signature only) — nothing to persist then.
                    # Claimed by line uuid + block index so a concurrent-tail
                    # race can't double-persist (thinking has no tool id).
                    t = block.get("thinking", "")
                    if t.strip():
                        luid = obj.get("uuid") or ""
                        if luid and not buf.claim(f"think:{luid}:{bi}"):
                            continue
                        persist_event(task_store, chat_id,
                                      {"type": "thinking", "content": t})
                        persisted += 1
                elif btype == "tool_use":
                    persisted += _on_tool_use(buf, reg, task_store, chat_id,
                                              block, open_questions)
            # Track the last assistant message's turn-boundary signal.
            sr = msg.get("stop_reason")
            if sr:
                last_stop_reason = sr
                last_assistant_text = "".join(msg_text_parts)
                last_signal = "end_turn" if sr == "end_turn" else "tool_use"

    # Question fold: an AskUserQuestion still unanswered at batch end means the
    # harness is blocked on the dialog — close the turn (headless parity: the
    # -p pump's turn ends right after the question card) so the live dot
    # clears and the "needs your input" ping can fire. The answer's Enter
    # triggers the submit tails and the continuing assistant lines reopen.
    # Never overrides a REAL later signal: "user" (question dismissed, new
    # prompt) and "end_turn" (ESC interrupt marker / real end) win — those
    # paths must not read as question-parked.
    question_pending = bool(open_questions) and last_signal in ("tool_use", None)
    if question_pending:
        last_signal = "end_turn"

    # Backfill the chat title from the first user prompt — interactive chats
    # can't title at send-time (the prompt rides the PTY). Only when still
    # untitled, so we never overwrite an existing title and it's idempotent
    # across batches (a later batch finds the title already set).
    title_set = False
    if first_user_text and chat_id:
        try:
            rec = task_store.get_chat(chat_id)
            if rec and not (rec.get("title") or "").strip():
                new_title = _title_from_prompt(first_user_text)
                if new_title:
                    task_store.update_chat(chat_id, title=new_title)
                    title_set = True
                    # Push to the user's dashboards so the sidebar updates now
                    # (vs waiting for a navigation refetch). Best-effort.
                    try:
                        from services.notifications import notification_manager
                        notification_manager.broadcast_chat_title(
                            rec.get("user_sub", ""), chat_id, new_title,
                            agent=rec.get("agent") or "",
                        )
                    except Exception:
                        pass
        except Exception:
            logger.exception("transcript title backfill failed for %s", session_id[:8])

    usage_rows = 0
    if usage_by_model and chat_id:
        usage_rows = record_batch_usage(session_id, chat_id, usage_by_model,
                                        task_store)

    if persisted:
        logger.info(
            "interactive %s: persisted %d transcript message(s) to chat %s%s",
            session_id[:8], persisted, chat_id, " (titled)" if title_set else "",
        )
    return {
        "persisted": persisted,
        "title_set": title_set,
        # Interactive completion signal (consumed by interactive_session).
        "turn_complete": last_stop_reason == "end_turn",
        "last_message": last_assistant_text,
        "last_signal": last_signal,
        # Turn parked on an unanswered AskUserQuestion (folded to end_turn
        # above) — the session words the end-of-turn ping as a question.
        "question_pending": question_pending,
        # A compact_boundary landed in this batch — interactive_session runs
        # turn-end effects when the session is idle, and a MANUAL trigger
        # additionally closes a stale-open turn (see _post_batch_effects).
        "compacted": compacted,
        "compact_trigger": compact_trigger,
        # usage_records rows written for this batch (0 = no new usage lines).
        "usage_rows": usage_rows,
    }
