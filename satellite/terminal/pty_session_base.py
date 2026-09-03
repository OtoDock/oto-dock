"""Shared base for the satellite's interactive PTY sessions.

The OS-agnostic machinery common to the Claude (:mod:`pty_session`) and Codex
(:mod:`codex_pty_session`) interactive sessions:

- the PTY spawn + output/exit wiring (``pty_relay`` on Unix / ``winpty_relay``
  ConPTY on Windows, picked by ``sys.platform`` — both expose the same
  ``spawn_pty``/handle interface);
- proxy-driven I/O (``write`` ← ``pty_input``, ``resize`` ← ``pty_resize``,
  ``close`` ← ``pty_close``);
- the transcript-forwarding tail loop: poll the CLI's session JSONL, read new
  whole lines by byte offset, and forward them to the proxy as ``transcript_lines``
  frames (the proxy persists to ``chat_messages`` + derives turn-complete). The
  satellite owns the location of the file (it controls ``CLAUDE_CONFIG_DIR`` /
  ``CODEX_HOME``), so it finds it path-agnostically — the design that also serves
  the future ``otodock`` CLI.

Subclasses implement :meth:`start` (write the CLI's config tree + build the
interactive argv + call :meth:`_wire_and_spawn`) and :meth:`_locate_transcript_file`
(Claude finds ``<id>.jsonl`` by name; Codex discovers its rollout).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable

from ..host import env_hygiene, path_translator
from ..transport import file_sync
from . import terminal_queries

logger = logging.getLogger("satellite")

# Transcript forwarding: how often to poll this session's transcript JSONL for
# new lines to forward to the proxy. The file only grows during a turn, so a
# sub-second poll keeps history landing within a couple seconds of a turn end.
# Cheap (a stat + delta read).
_TRANSCRIPT_POLL_S = 0.8

# Server-prompt injection (proxy `pty_inject`): the PROXY owns the turn-state
# decision (it parses end_turn from the forwarded transcript lines); this side
# re-checks for a BOUNDED window what only it can see — the local terminal's
# pending input line + transcript burst state — then NACKs rather than decaying
# that decision by injecting much later.
_INJECT_RETRY_S = 1.0
_INJECT_MAX_WAIT_S = 12.0
# No local keystroke for this long before an injection (the user isn't
# mid-thought on the line).
_INJECT_LOCAL_QUIET_S = 2.0
# Input classification carry: an incomplete trailing escape sequence is held
# for the next chunk instead of being misread as typing. Bounds a runaway
# unterminated OSC/DCS body — past this, the tail is dropped from
# classification (never from the PTY write path).
_INPUT_CARRY_MAX = 256
# Gap between the pasted body and the submitting Enter — the TUI needs to
# ingest the paste first (mirrors the proxy's warm-submit split write).
_INJECT_ENTER_DELAY_S = 0.3


def resolve_work_cwd(work_cwd: str, agent_dir: Path, cwd_relative: str) -> Path:
    """Resolve the working directory for a spawned CLI — shared by the interactive
    PTY sessions (:class:`BasePtySession`) AND the headless ``-p`` CLI session
    (:mod:`cli_session`), so an `otodock` chat resumes in the SAME folder whichever
    path the dashboard picks (Claude keys its session JSONL by cwd → a `-p` resume
    in the wrong cwd 404s with "No conversation found").

    Default (no ``work_cwd``): ``agent_dir / cwd_relative`` inside the synced tree,
    created if missing. With an absolute ``work_cwd`` (otodock-CLI / arbitrary
    folder): spawn THERE — it must already EXIST (never ``mkdir`` an out-of-tree
    path; a typo must not silently create one). Guards (defense-in-depth; the proxy
    is the authoritative gate): reject the filesystem root, anything inside/at the
    OtoDock data dir (``otodock_dir()`` — its agents/mcps/venv tree is the
    platform's, not a project), and any dir ABOVE the home directory. The user's
    HOME itself and normal folders under it (Desktop, projects, …) ARE allowed,
    even though OtoDock lives under home. Raises ``ValueError`` on an invalid path."""
    work_cwd = (work_cwd or "").strip()
    if not work_cwd:
        cwd = agent_dir / cwd_relative
        cwd.mkdir(parents=True, exist_ok=True)
        return cwd
    p = Path(work_cwd)
    if not p.is_absolute():
        raise ValueError(f"work_cwd must be an absolute path, got {work_cwd!r}")
    if not p.is_dir():
        raise ValueError(f"work_cwd does not exist or is not a directory: {work_cwd}")
    real = Path(os.path.realpath(p))
    if real == Path(real.anchor):  # filesystem / drive root ("/", "C:\\")
        raise ValueError("work_cwd may not be the filesystem root")
    from ..config import otodock_dir
    oto_root = Path(os.path.realpath(otodock_dir()))
    if real == oto_root or oto_root in real.parents:
        raise ValueError("work_cwd may not be inside the OtoDock data directory")
    home = Path(os.path.realpath(Path.home()))
    if real in home.parents:  # cwd is ABOVE $HOME (too broad)
        raise ValueError("work_cwd may not be an ancestor of the home directory")
    return p


class BasePtySession:
    """Common spine for a CLI TUI subprocess on a PTY, driven by the proxy over
    the WS. Not used directly — see ``PtySession`` (Claude) / ``CodexPtySession``."""

    execution_path = ""  # subclass sets ("claude-code-cli" / "codex-cli")

    def __init__(
        self,
        session_id: str,
        agent_dir: Path,
        config: dict,
        sat_config: "object",
        ws: "object",
        *,
        rows: int = 24,
        cols: int = 80,
    ):
        self.session_id = session_id
        self.agent_dir = agent_dir
        self.agent_slug = agent_dir.name
        self.config = config
        self.sat_config = sat_config
        self._ws = ws
        self.rows = rows
        self.cols = cols
        self.pty = None
        self.pid: int | None = None
        self._cwd: Path | None = None
        self._file_snapshot: dict[str, str] = {}
        # File write-back: set when the transcript forwarded new lines (the CLI
        # is mid-turn); the first QUIET poll after a burst triggers the scan —
        # the PTY twin of the -p path's end-of-turn detect_file_changes.
        self._files_dirty = False
        # Transcript forwarding state.
        self._tail_task: "asyncio.Task | None" = None
        self._tail_stop = False
        self._transcript_path: Path | None = None
        self._transcript_offset = 0
        # On --resume the JSONL/rollout already holds the prior conversation (already
        # in the proxy DB), so skip past it on first locate and forward only new lines.
        self._transcript_skip_existing = False
        # Invoked when the child PROCESS exits on its own (EOF/crash)
        # so session_manager can drop this session from `pty_sessions` — keeps the
        # post-auth `pty_alive` set accurate (no dead-session re-adopt). Set by
        # session_manager.pty_open; the proxy-driven pty_close pops the dict
        # itself, so this fires only on a child-initiated exit.
        self._on_dead: "Callable[[], None] | None" = None
        # otodock-CLI: optional TEE of PTY output/exit to a local control-socket
        # client (the `otodock` terminal on this machine). Set via
        # :meth:`attach_local_relay`; both are best-effort and never block or break
        # the proxy mirror (the dashboard view + transcript are unaffected).
        self._local_relay: "Callable[[bytes], None] | None" = None
        self._local_exit: "Callable[[int | None], None] | None" = None
        # Server-prompt injection (proxy `pty_inject`): local input-line state
        # (fed by :meth:`note_local_input` from the T_INPUT path), one
        # injection in flight at a time, and a recent-id ring so a proxy
        # re-send after a lost result frame re-ACKs without re-injecting.
        self._local_line_dirty = False
        self._last_local_input = 0.0
        self._input_carry = b""
        self._injecting = False
        self._recent_injects: deque[str] = deque(maxlen=8)

    # --- otodock-CLI local relay --------------------------------------------
    def attach_local_relay(
        self,
        on_output: "Callable[[bytes], None]",
        on_exit: "Callable[[int | None], None]",
    ) -> None:
        """Tee this PTY's output/exit to a local `otodock` terminal in addition to
        the proxy mirror. Idempotent (last attach wins)."""
        self._local_relay = on_output
        self._local_exit = on_exit

    def detach_local_relay(self) -> None:
        """Stop teeing to the local terminal (it disconnected). The PTY + the proxy
        mirror keep running — the chat stays dashboard-resumable."""
        self._local_relay = None
        self._local_exit = None

    # --- server-prompt injection (proxy `pty_inject`) -------------------------
    def note_local_input(self, data: bytes) -> None:
        """Track the local `otodock` terminal's input-line state for the
        injection gates. Terminal auto-replies (the client terminal answers the
        TUI's per-frame ``CSI 6n`` — those CPR digits ride T_INPUT continuously
        during repaints) and mouse reports are NOT typing; a chunk ENDING with
        CR/NL is a submit (interior paste newlines don't submit); Ctrl-C
        empties the line. Called from the T_INPUT path only — proxy-driven
        ``pty_input`` writes must not count as local typing.

        Auto-replies arrive in arbitrary socket-read chunks: a CPR split across
        two reads leaves residue that reads as typing and STICKS ``line_dirty``
        until the next Enter — that stalled injections for tens of minutes
        (2026-07-08). An incomplete trailing escape sequence is therefore held
        in a small carry and classified joined with the next chunk. A bare user
        Esc parks in the carry — benign, Esc never dirties a line. This path is
        observational only; the actual bytes always reach the PTY unmodified."""
        data = self._input_carry + data
        complete, partial = terminal_queries.split_trailing_partial(data)
        self._input_carry = partial if len(partial) <= _INPUT_CARRY_MAX else b""
        typed = terminal_queries.MOUSE_RE.sub(
            b"", terminal_queries.strip_replies(complete),
        )
        if not typed:
            return
        self._last_local_input = time.monotonic()
        if b"\x03" in typed or typed.endswith((b"\r", b"\n")):
            self._local_line_dirty = False
        else:
            self._local_line_dirty = True

    def _inject_blocked(self) -> "str | None":
        """The first injection gate blocking right now, or None when clear.
        ``session_gone``/``not_attached`` are terminal (NACK immediately);
        the rest are retryable within the bounded window."""
        if self.pty is None or getattr(self.pty, "closed", False):
            return "session_gone"
        if self._local_relay is None:
            # A detach race: the local terminal is gone, so the proxy's
            # composer tracking (dashboard input) is authoritative again —
            # injecting here would bypass it.
            return "not_attached"
        if self._transcript_path is None:
            return "not_ready"  # nothing established yet (fresh spawn)
        if self._files_dirty:
            return "transcript_busy"  # forwarding a burst — mid-turn
        if self._local_line_dirty:
            return "line_dirty"
        if time.monotonic() - self._last_local_input < _INJECT_LOCAL_QUIET_S:
            return "typing"
        return None

    async def inject_prompt(self, text: str, inject_id: str, source: str = "") -> None:
        """Write a server prompt (delegate result …) into the CLI's stdin as a
        VISIBLE bracketed paste + Enter — the user sees exactly what was
        injected in their terminal. Gated on the local input line being clean
        and the transcript quiet, for a bounded window; then NACKs ``busy`` so
        the proxy re-queues and waits for the next turn-complete under its own
        gates. Always answers with a ``pty_inject_result`` frame."""
        if inject_id in self._recent_injects:
            await self._send_inject_result(inject_id, True, "duplicate")
            return
        if self._injecting:
            await self._send_inject_result(inject_id, False, "busy")
            return
        self._injecting = True
        try:
            deadline = time.monotonic() + _INJECT_MAX_WAIT_S
            while True:
                reason = self._inject_blocked()
                if reason is None:
                    break
                if reason in ("session_gone", "not_attached"):
                    await self._send_inject_result(inject_id, False, reason)
                    return
                if time.monotonic() >= deadline:
                    await self._send_inject_result(inject_id, False, "busy")
                    return
                await asyncio.sleep(_INJECT_RETRY_S)
            raw = text.encode("utf-8").replace(b"\r\n", b"\n")
            # The injected text is LLM-generated content from ANOTHER session
            # (delegate results etc.) — strip ESC so an embedded \x1b[201~
            # can't terminate the paste frame early and turn the remainder
            # into live keystrokes (submitting prompts / answering pending
            # permission dialogs as the user). Other C0 controls ride inside
            # a paste harmlessly; ESC is the only frame-breaking byte.
            raw = raw.replace(b"\x1b", b"")
            self.write(b"\x1b[200~" + raw + b"\x1b[201~")
            await asyncio.sleep(_INJECT_ENTER_DELAY_S)
            if self.pty is None or getattr(self.pty, "closed", False):
                await self._send_inject_result(inject_id, False, "session_gone")
                return
            self.write(b"\r")
            self._recent_injects.append(inject_id)
            await self._send_inject_result(inject_id, True, "")
            logger.info(
                "PTY %s: injected server prompt [%s] (%d bytes)",
                self.session_id, source or "?", len(raw),
            )
        finally:
            self._injecting = False

    def _send_inject_result(self, inject_id: str, ok: bool, reason: str):
        return self._ws.enqueue_send({
            "type": "pty_inject_result",
            "session_id": self.session_id,
            "inject_id": inject_id,
            "ok": ok,
            "reason": reason,
        })

    async def start(self) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # --- shared cwd resolution ----------------------------------------------
    def _resolve_work_cwd(self) -> Path:
        """Working directory for the spawned CLI — delegates to the module-level
        :func:`resolve_work_cwd` (shared with the ``-p`` :mod:`cli_session`).
        Config dirs + username derivation stay keyed on ``cwd_relative``
        (agent_dir-rooted); only the working directory moves. Raises ``ValueError``
        on an invalid ``work_cwd`` — :meth:`session_manager.pty_open` catches it
        and acks a clean error to the caller."""
        return resolve_work_cwd(
            self.config.get("work_cwd") or "",
            self.agent_dir,
            self.config["cwd_relative"],
        )

    # --- shared env setup ----------------------------------------------------
    def _base_env(self) -> dict[str, str]:
        """Curate the operator's ambient secrets → overlay the platform env → set
        the common interactive vars (a TUI with no TERM renders garbage; the -p
        path doesn't need it). Subclasses add the CLI-specific dir var (
        CLAUDE_CONFIG_DIR / CODEX_HOME) before :meth:`_translate_env`."""
        env = env_hygiene.curate_satellite_env(os.environ)
        env.update(self.config.get("env", {}))
        env["OTO_SESSION_ID"] = self.session_id
        env.setdefault("PYTHONUTF8", "1")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        # otodock-CLI: honor the client terminal's forwarded $TERM so the
        # remote PTY renders to match the user's real terminal; otherwise force
        # xterm-256color for the dashboard xterm.js, WITH the truecolor hint —
        # xterm.js renders 24-bit, and without it the TUIs downgrade to
        # 256-color SGR (muddier theme colors, and the dashboard's truecolor
        # diff brand-tint never matches).
        client_term = (self.config.get("term") or "").strip()
        if client_term:
            env["TERM"] = client_term
        else:
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("COLORTERM", "truecolor")
        return env

    async def _materialize_session_files(self) -> dict[str, str]:
        """Fetch this session's secret files from the platform's session-file
        broker BEFORE spawn — SSH keys (injects ``OTO_SSH_KEY_DIR``) + OAuth
        token files landed at their agent-tree credentials_dir target — the
        same step the headless sessions run (interactive sessions used to skip
        it, so their bash had no ssh-hosts keys and credentials_dir MCPs had
        no tokens). No token in the payload → no-op. Wiped in :meth:`close`."""
        from ..sessions import session_files
        return await asyncio.to_thread(
            session_files.materialize, self.config, self.session_id,
            self.agent_dir,
        )

    def _translate_env(self, env: dict[str, str]) -> dict[str, str]:
        """Rewrite sandbox-virtual paths in the env to satellite-absolute (same
        convention as bwrap on local)."""
        username = path_translator.derive_username_from_cwd_relative(
            self.config.get("cwd_relative", ""),
        )
        return path_translator.translate_env(
            env,
            agent_dir=self.agent_dir,
            username=username,
            session_id=self.session_id,
            multi_value_envs=self.config.get("multi_value_envs") or {},
        )

    # --- spawn + wire output/exit back over the WS ---------------------------
    async def _wire_and_spawn(self, cmd: list[str], env: dict[str, str], cwd: str) -> None:
        """Spawn ``cmd`` under a PTY (OS-appropriate backend), stream its bytes to
        the proxy (``pty_output``), report exit (``pty_exit``), snapshot the agent
        dir (off-thread), and start the transcript tail loop."""
        # Pick the PTY backend for this OS, imported lazily so importing this module
        # never pulls in pty/fcntl/termios (Unix) or pywinpty (Windows) on the wrong
        # host. Both expose the same spawn_pty/handle interface.
        if sys.platform == "win32":
            from . import winpty_relay as pty_relay
        else:
            from . import pty_relay

        def _on_output(data: bytes):
            # Tee to a local otodock terminal first (non-blocking, best-effort
            # — a stuck local client must never stall the proxy mirror / PTY read).
            relay = self._local_relay
            if relay is not None:
                try:
                    relay(data)
                except Exception:
                    logger.debug("otodock local relay output failed", exc_info=True)
            # Ride the LOSSLESS pty lane (blocking put) — the backend
            # AWAITS this coroutine (Unix reader task) / blocks on it (Windows
            # reader thread), so a full lane pauses the read instead of dropping.
            return self._ws.enqueue_pty({
                "type": "pty_output",
                "session_id": self.session_id,
                "data_b64": base64.b64encode(data).decode(),
            })

        def _on_exit(code):
            # Tell a local otodock terminal the CLI exited (so it restores the
            # tty + exits with this code), before the proxy bookkeeping.
            exit_cb = self._local_exit
            if exit_cb is not None:
                try:
                    exit_cb(code)
                except Exception:
                    logger.debug("otodock local relay exit failed", exc_info=True)
            # Drop ourselves from session_manager.pty_sessions BEFORE
            # forwarding the exit, so a racing reconnect's pty_alive doesn't list a
            # dead session (child-initiated exit; the proxy pty_close pops it too).
            if self._on_dead is not None:
                try:
                    self._on_dead()
                except Exception:
                    logger.debug("pty _on_dead hook failed", exc_info=True)
            return self._ws.enqueue_send({
                "type": "pty_exit",
                "session_id": self.session_id,
                "code": code,
            })

        self.pty = pty_relay.spawn_pty(
            cmd,
            env=env,
            cwd=cwd,
            rows=self.rows,
            cols=self.cols,
            on_output=_on_output,
            on_exit=_on_exit,
        )
        self.pid = self.pty.pid
        logger.info(
            "PTY session started: pid=%s session=%s path=%s (%dx%d)",
            self.pid, self.session_id, self.execution_path, self.cols, self.rows,
        )
        # Snapshot files for change detection (parity with CLISession).
        # Off-thread — the hash walk must not stall the loop at session start.
        self._file_snapshot = await asyncio.to_thread(
            file_sync.snapshot_agent_dir, self.agent_dir,
        )
        # Tail the transcript JSONL + forward new lines to the proxy.
        self._tail_task = asyncio.create_task(self._transcript_tail_loop())

    # --- transcript forwarding (shared) --------------------------------------
    def _locate_transcript_file(self) -> "Path | None":  # pragma: no cover - abstract
        """Find this session's JSONL on disk. Claude: ``<id>.jsonl`` by name;
        Codex: discover the rollout. Returns None until the CLI has created it."""
        raise NotImplementedError

    def _read_new_complete_lines(self) -> list[str]:
        """Read whole JSONL lines appended since the last forward (by byte offset).
        A trailing partial line is left for the next poll. Resets on a shrink (the
        CLI only appends, so this is just defensive)."""
        path = self._transcript_path
        if path is None:
            return []
        try:
            size = path.stat().st_size
        except OSError:
            return []
        if size < self._transcript_offset:
            self._transcript_offset = 0  # rotated/truncated → re-read
        if size <= self._transcript_offset:
            return []
        try:
            with open(path, "rb") as fh:
                fh.seek(self._transcript_offset)
                data = fh.read()
        except OSError:
            return []
        last_nl = data.rfind(b"\n")
        if last_nl < 0:
            return []  # only a partial line so far
        complete = data[: last_nl + 1]
        self._transcript_offset += len(complete)
        text = complete.decode("utf-8", errors="replace")
        return [ln for ln in text.split("\n") if ln.strip()]

    async def _forward_transcript_delta(self) -> bool:
        """Forward newly appended transcript lines. Returns True when lines were
        forwarded — the tail loop's signal that the CLI is mid-turn (arms the
        quiescence file scan below)."""
        if self._transcript_path is None:
            self._transcript_path = self._locate_transcript_file()
            if self._transcript_path is None:
                return False
            if self._transcript_skip_existing:
                # Resume: prior history is already in the proxy DB → start past it.
                try:
                    self._transcript_offset = self._transcript_path.stat().st_size
                except OSError:
                    self._transcript_offset = 0
                self._transcript_skip_existing = False
                return False
        lines = self._read_new_complete_lines()
        if lines:
            await self._ws.enqueue_send({
                "type": "transcript_lines",
                "session_id": self.session_id,
                "lines": lines,
            })
        return bool(lines)

    async def _scan_and_forward_files(self) -> None:
        """Detect + forward agent-tree file changes — the PTY twin of the ``-p``
        path's end-of-turn scan (``session_manager._stream`` →
        ``detect_file_changes``). A TUI has no ``send_message`` boundary, so the
        tail loop calls this on transcript QUIESCENCE (first quiet poll after a
        burst of new lines ≈ the turn just ended) and ``close()`` forces a final
        one. Authorization is unchanged and proxy-side: the ``file_changed``
        applier vets every path against the session's authenticated
        SecurityContext (``can_write_back``), identical to ``-p`` sessions.
        The hash walk runs in a thread so a large tree never stalls the shared
        event loop; ``detect_changes`` refreshes the snapshot in place, and
        incoming platform pushes refresh it too (``session_manager.file_push``)
        so a push never echoes back as a phantom write-back."""
        try:
            changes = await asyncio.to_thread(
                file_sync.detect_changes, self.agent_dir, self._file_snapshot,
            )
        except Exception:
            logger.debug(
                "PTY %s: file scan failed", self.session_id, exc_info=True,
            )
            return
        for change in changes:
            await self._ws.enqueue_send({
                "type": "file_changed",
                "session_id": self.session_id,
                "agent_slug": self.agent_slug,
                **change,
            })

    async def _transcript_tail_loop(self) -> None:
        try:
            while not self._tail_stop:
                await asyncio.sleep(_TRANSCRIPT_POLL_S)
                forwarded = await self._forward_transcript_delta()
                if forwarded:
                    self._files_dirty = True
                elif self._files_dirty:
                    # First quiet poll after a burst — the turn (very likely)
                    # ended; sync what it wrote. Clear BEFORE the scan so new
                    # activity DURING the scan re-arms another pass.
                    self._files_dirty = False
                    await self._scan_and_forward_files()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.debug(
                "PTY %s: transcript tail loop error", self.session_id, exc_info=True
            )

    # --- proxy-driven I/O (shared) -------------------------------------------
    def write(self, data: bytes) -> None:
        if self.pty is not None:
            self.pty.write(data)

    def resize(self, rows: int, cols: int) -> None:
        self.rows, self.cols = rows, cols
        if self.pty is not None:
            self.pty.resize(rows, cols)

    def force_repaint(self) -> None:
        """Dual-control: guarantee the TUI repaints on a controller take-over.
        The new terminal's size may EQUAL the PTY's current size, and a same-size
        ``TIOCSWINSZ`` emits no ``SIGWINCH`` — so an alt-screen TUI would keep
        showing the replayed (stale/mid-frame) scrollback. Nudge the winsize
        off-by-one and back so a ``SIGWINCH`` always fires and the TUI redraws."""
        if self.pty is None:
            return
        try:
            self.pty.resize(self.rows, max(1, self.cols - 1))
            self.pty.resize(self.rows, self.cols)
        except Exception:
            logger.debug("PTY %s: force_repaint nudge failed", self.session_id, exc_info=True)

    async def close(self) -> None:
        # Stop the tail loop, then do a FINAL forward so the last lines the CLI
        # wrote before teardown still reach the proxy.
        self._tail_stop = True
        if self._tail_task is not None:
            self._tail_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._tail_task
            self._tail_task = None
        try:
            await self._forward_transcript_delta()
        except Exception:
            logger.debug(
                "PTY %s: final transcript flush failed", self.session_id, exc_info=True
            )
        # Final write-back scan — the last burst may have had no quiet poll
        # after it (and a file can change without a transcript line at all).
        try:
            await self._scan_and_forward_files()
        except Exception:
            logger.debug(
                "PTY %s: final file scan failed", self.session_id, exc_info=True
            )
        self._on_close()
        if self.pty is not None:
            self.pty.terminate()
        # Same teardown the headless sessions run — the CLI is gone, so its
        # session-secret files (ssh keys / OAuth token files) go too.
        from ..sessions import session_files
        session_files.wipe(self.session_id)

    def _on_close(self) -> None:
        """Subclass hook for teardown that must run before the PTY is terminated
        (e.g. releasing a pinned rollout). Default no-op."""
        return
