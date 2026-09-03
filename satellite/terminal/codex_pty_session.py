"""Interactive PTY session for the satellite daemon — Codex.

The Codex twin of :class:`PtySession`: instead of the ``codex app-server`` JSON-RPC
daemon (see ``codex_session.CodexSession``, the -p path), this spawns the native
``codex`` Ratatui TUI under a PTY and is driven by the proxy over the WS. The shared
spine (PTY spawn + output/exit wiring, ``pty_input``/``pty_resize``/``pty_close``,
and the transcript-forwarding tail loop) lives in :class:`BasePtySession`; this
subclass adds only the Codex-specific bits:

- the ``.codex`` config tree (config.toml + AGENTS.md + auth.json + hooks), written
  exactly as ``CodexSession`` does (reusing its helpers);
- the INTERACTIVE command — ``codex --no-alt-screen -s <sandbox> -a <approval>
  --dangerously-bypass-hook-trust -C <cwd> [-m <model>] [-c model_reasoning_effort=…]``
  with the cold first prompt as a trailing positional arg (a fresh TUI auto-runs it),
  or ``codex resume <thread_id> …`` to continue an existing thread (the continuation
  prompt rides the PTY flush);
- discovering THIS session's rollout JSONL under ``CODEX_HOME`` (the file is named
  ``rollout-<ts>-<thread_uuid>.jsonl``, not by our session id — see
  :meth:`_locate_transcript_file`), then forwarding new lines via the inherited tail
  loop. The proxy's ``codex_rollout_tailer.tail_lines`` persists them + captures the
  thread id + signals turn-complete.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import sys
import time
from pathlib import Path

from ..host import path_translator
from .._vendored.codex_approvals import approval_for_sandbox
from ..sessions.codex_session import (
    _validate_config_toml, _write_codex_hook_scripts, _write_codex_hooks,
)
from ..config import otodock_dir
from .pty_session_base import BasePtySession

logger = logging.getLogger("satellite")

# Fresh-spawn lower bound slack: Codex writes its rollout within ~1 s of spawn; allow
# clock/race slack so we never miss it but still exclude a sibling chat's older
# rollout. Mirrors the proxy's codex_rollout_tailer._RESOLVE_MTIME_MARGIN_S.
_RESOLVE_MTIME_MARGIN_S = 5.0

# Rollouts pinned by a live CodexPtySession on this satellite, so two concurrent
# interactive Codex sessions for the SAME (user, agent) — which share one CODEX_HOME
# AND cwd — can't both grab (and double-forward) the same rollout. Mirrors the
# proxy tailer's ``_resolved``/``others`` exclusion.
_pinned_rollouts: set[str] = set()


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


def _build_codex_config_toml(trusted_cwd: str, mcp_toml: str) -> str:
    """Build the interactive `codex` config.toml — satellite twin of the proxy's
    ``core.layers.codex.layer._write_config_toml(interactive=True, trusted_cwd=…)``.

    The proxy ships ONLY the ``[mcp_servers.*]`` sections (``mcp_registry._servers_to_toml``);
    the interactive HEADER is added here so a remote Codex TUI matches local:
      - ``project_doc_max_bytes`` — large AGENTS.md system prompts (default cap 32 KB);
      - ``check_for_update_on_startup`` off — the TUI's update prompt would break the
        version pin (`npm install -g` on Enter); ``suppress_unstable_features_warning``
        pairs with the request_user_input feature flag below;
      - ``[memories]`` off — the otodock memory system is the single source of truth;
      - ``[features] plugins = false`` — skip Codex's curated-plugins startup sync (lean-start);
      - ``[features] hooks = true`` — run the PreToolUse ``permission_gate`` FLOOR (the
        command-level deny floor; WITHOUT this the floor is silently off on remote!);
      - ``[projects."<cwd>"] trust_level = "trusted"`` — skip Codex's "Do you trust the
        contents of this directory?" startup prompt. Keyed by the SATELLITE-absolute cwd
        (the satellite owns it — no path translation needed), under BOTH separator forms
        (Windows codex may normalise to forward slashes; the set collapses to one on Unix).
    KEEP IN SYNC with the proxy's ``_write_config_toml``.
    """
    parts = [
        "project_doc_max_bytes = 300000",
        # Root keys stay above the first [table] header. check_for_update
        # guards the pin (the TUI's update prompt runs `npm install -g` on
        # Enter); the suppress key silences the "under-development features"
        # warning our request_user_input flag would otherwise print.
        "check_for_update_on_startup = false",
        "suppress_unstable_features_warning = true",
        "[memories]",
        "use_memories = false",
        "generate_memories = false",
        "[features]",
        "plugins = false",
        "hooks = true",
        # request_user_input in the default collaboration mode (interactive
        # TUI only — this builder never serves headless); the proxy-side
        # rollout tailer folds the pending call to a question-parked turn.
        "default_mode_request_user_input = true",
    ]
    if trusted_cwd:
        for key in {trusted_cwd, trusted_cwd.replace("\\", "/")}:
            esc = key.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'[projects."{esc}"]')
            parts.append('trust_level = "trusted"')
    if mcp_toml.strip():
        # The remote ``startup_timeout_sec`` floor (and per-MCP overrides for
        # heavy servers like google-workspace) is applied PROXY-side in
        # ``remote_execution._rewrite_mcp_toml_for_remote`` — which covers this
        # interactive TUI path AND the app-server path — so the MCP TOML already
        # arrives with remote-safe timeouts. Append it (minus any LEADING
        # [features] block an older proxy prepends for headless dashboard
        # chats: the preamble above already declares [features] — with the
        # same request_user_input flag — and a second [features] table is a
        # DUPLICATE KEY the strict TUI hard-exits on with a blank terminal).
        parts.append(_strip_leading_features_block(mcp_toml.strip()))
    return "\n".join(parts) + "\n"


def _strip_leading_features_block(mcp_toml: str) -> str:
    """Drop a leading ``[features] …`` block (up to the next ``[table]``
    header) from a proxy-shipped MCP TOML. Lossless today: the only key a
    proxy ever prepends there is ``default_mode_request_user_input``, which
    the interactive preamble already sets."""
    lines = mcp_toml.split("\n")
    if not lines or lines[0].strip() != "[features]":
        return mcp_toml
    for i in range(1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped != "[features]":
            return "\n".join(lines[i:])
    return ""


def _cwd_match(meta_cwd: str, cwd: str) -> bool:
    """True if the rollout's recorded cwd matches this session's cwd, normalised
    (path separators + case) so a Windows backslash/case difference still matches."""
    if not meta_cwd or not cwd:
        return False
    return (
        os.path.normcase(os.path.normpath(meta_cwd))
        == os.path.normcase(os.path.normpath(cwd))
    )


class CodexPtySession(BasePtySession):
    """A Codex Ratatui TUI subprocess on a PTY, driven by the proxy over the WS."""

    execution_path = "codex-cli"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._codex_dir: Path | None = None
        self._started_at = 0.0  # spawn time → excludes a sibling chat's older rollout
        self._thread_id = (self.config.get("thread_id") or "").strip()
        # Resume iff the proxy said so AND a thread id is known (codex resume <tid>).
        self._resume = bool(self.config.get("resume")) and bool(self._thread_id)
        # On resume the rollout already holds the prior conversation (already in the
        # proxy DB) → skip past it on first locate and forward only new lines.
        self._transcript_skip_existing = self._resume
        self._transcript_pin: str | None = None

    async def start(self) -> None:
        """Write the .codex config tree + spawn the interactive Codex TUI under a PTY."""
        # cwd may be an arbitrary absolute folder (otodock-CLI); the .codex
        # dir stays agent_dir-rooted via codex_dir_relative regardless.
        self._cwd = self._resolve_work_cwd()
        self._codex_dir = self.agent_dir / self.config["codex_dir_relative"]
        self._codex_dir.mkdir(parents=True, exist_ok=True)
        self._apply_resume_guard()
        # Self-heal a foreign/stale codex state DB BEFORE launch. The bare `codex`
        # TUI aborts with an INTERACTIVE "migration N … has been modified / Repair
        # Codex local data? [y/N]" prompt when CODEX_HOME holds a state DB written
        # by a different codex version (or the app-server vs bare-TUI schema) —
        # which would hang the PTY waiting for a y/N we can't supply. The app-server
        # path self-heals this reactively (codex_session._reset_codex_state); the
        # TUI can't, so we reset proactively. Regenerable + host-local (never
        # synced); the rollouts in sessions/ are untouched, so history/resume survive.
        self._reset_codex_state()

        # --- write the .codex/ config tree (mirror CodexSession.start) --------
        _write_codex_hook_scripts(self._codex_dir, self.config.get("hook_scripts", {}))
        (self._codex_dir / "AGENTS.md").write_text(
            self.config.get("agents_md_content", self.config.get("system_prompt", ""))
        )
        toml_content = self.config.get("mcp_config_toml", "")
        if toml_content:
            otodock_fwd = str(otodock_dir()).replace("\\", "/")
            home_fwd = str(Path.home()).replace("\\", "/")
            toml_content = toml_content.replace("~/.oto-dock", otodock_fwd)
            toml_content = toml_content.replace("~/", home_fwd + "/")
            # Codex doesn't propagate the daemon env to MCP children, so the
            # config.toml env tables are the only copy — translate their
            # sandbox-virtual paths to satellite-absolute (else an MCP that opens
            # one at startup, e.g. google-workspace's credentials dir, dies with
            # "connection closed: initialize response"). Mirrors `_translate_env`.
            toml_content = path_translator.translate_codex_mcp_env_paths(
                toml_content,
                agent_dir=self.agent_dir,
                username=path_translator.derive_username_from_cwd_relative(
                    self.config.get("cwd_relative", ""),
                ),
                session_id=self.session_id,
            )
            from ..sessions.codex_session import _inject_display_env_toml
            toml_content = _inject_display_env_toml(toml_content)
            from ..sessions.mcp_interceptor import wrap_interceptor_in_mcp_config_toml
            toml_content = wrap_interceptor_in_mcp_config_toml(toml_content)
        # ALWAYS write config.toml: the proxy ships only the [mcp_servers.*] sections,
        # so the interactive header (large-prompt limit, memory-off, lean-start, the
        # hooks FLOOR, and the cwd-trust that skips Codex's "Do you trust this
        # directory?" prompt) is added here, keyed by the SATELLITE-absolute cwd.
        # Needed even with no MCP servers.
        config_toml_text = _build_codex_config_toml(str(self._cwd), toml_content)
        _validate_config_toml(config_toml_text, self._codex_dir / "config.toml")
        (self._codex_dir / "config.toml").write_text(config_toml_text)
        auth_json = self.config.get("auth_json")
        if auth_json:
            auth_path = self._codex_dir / "auth.json"
            auth_path.write_text(json.dumps(auth_json, indent=2))
            # Live OAuth tokens — owner-only (multi-user satellite host).
            auth_path.chmod(0o600)
        _write_codex_hooks(self._codex_dir)

        # --- env (mirror CodexSession + the bare-TUI interactive vars) --------
        # The shared base set OTO_SESSION_ID + UTF-8 + TERM. Add CODEX_HOME and the
        # two interactive flags the local codex layer sets: OTO_INTERACTIVE (the
        # PostToolUse forwarder no-ops; decide_tool_permission returns "ask" for
        # the ask-tier — the deny-only gate stays silent on it, so Codex's own
        # -a on-request prompts), and OTO_HOOK_DENY_ONLY
        # (Codex's PreToolUse hook rejects permissionDecision:"allow", so the gate
        # emits JSON only to DENY; the hard-deny FLOOR still enforces).
        env = self._base_env()
        env.update(await self._materialize_session_files())
        env["CODEX_HOME"] = str(self._codex_dir)
        env["OTO_INTERACTIVE"] = "1"
        env["OTO_HOOK_DENY_ONLY"] = "1"
        # Interpreter for the Windows .cmd hook wrappers (codex_hook_command)
        # — the batch text stays ASCII by resolving python through the env.
        env["OTO_HOOK_PY"] = sys.executable
        env = self._translate_env(env)

        # --- build the INTERACTIVE command ------------------------------------
        from ..host.cli_versions import resolve_spawn_bin_async
        codex_bin = await resolve_spawn_bin_async(
            "codex", self.sat_config.codex_bin, for_pty=True,
        )
        sandbox_mode = self.config.get("sandbox_mode", "workspace-write")
        approval = approval_for_sandbox(sandbox_mode)
        # --no-alt-screen = inline mode (matches Claude's inline model + keeps xterm
        # scrollback); the command-level FLOOR runs via .codex/hooks.json with
        # --dangerously-bypass-hook-trust (the platform vets the hook source).
        #
        # NOTE: the Windows-remote Codex "cursor jumping / line-by-line redraw" is a
        # KNOWN, unfixed Codex CLI bug in its Windows TUI renderer (openai/codex
        # #9081 "cursor blink speeds up and jumps during redraw", closed as
        # not-planned; same family as #24421/#23740 post-0.130.0). It reproduces in
        # real Windows terminals (Windows Terminal/cmd/pwsh) with plain `codex`, so
        # it is NOT a transport/ConPTY-piping bug on our side — our xterm faithfully
        # mirrors Codex's broken Windows output. Toggling alt-screen does NOT help
        # (tested live), so we keep the inline default everywhere. Recommend Claude
        # for Windows-remote interactive; Codex interactive is clean on Linux + local.
        flags = [
            "--no-alt-screen",
            "-s", sandbox_mode,
            "-a", approval,
            "--dangerously-bypass-hook-trust",
            "-C", str(self._cwd),
        ]
        model = self.config.get("model", "")
        if model:
            flags += ["-m", model]
        effort = self.config.get("effort", "")  # already codex-mapped by the proxy
        if effort:
            flags += ["-c", f'model_reasoning_effort="{effort}"']
        if self._resume:
            # Resume the existing rollout/thread; the continuation prompt arrives via
            # the PTY flush (proxy write_input), not argv.
            cmd = [codex_bin, "resume", self._thread_id, *flags]
        else:
            cmd = [codex_bin, *flags]
            # Cold first prompt as the trailing positional PROMPT → codex auto-runs
            # it once MCP warm finishes (deterministic first-turn submit; the PTY
            # type-then-Enter race is unreliable during Codex's warm).
            first_prompt = (self.config.get("interactive_first_prompt") or "").strip()
            if first_prompt:
                # SINGLE-LINE the argv prompt. The browser time-injection prefixes
                # "[Current time: …]\n\n<text>", and a MULTI-LINE prompt pre-filled in
                # Codex's composer makes its inline (--no-alt-screen) TUI repaint
                # erratically during the MCP warm over a laggy WS (the cursor bounces /
                # the UI redraws line-by-line — the remote "cursor jumping" report).
                # Collapse newlines so it occupies ONE composer line. (Headless -p and
                # Claude keep the prompt verbatim — neither pre-fills a TUI composer.)
                first_prompt = " ".join(
                    ln.strip() for ln in first_prompt.splitlines() if ln.strip()
                )
                cmd.append(first_prompt)

        # Record spawn time BEFORE the PTY starts so rollout discovery can exclude a
        # sibling chat's older rollout (the rollout is written ~1s after spawn).
        self._started_at = time.time()
        await self._wire_and_spawn(cmd, env, str(self._cwd))
        if self._resume_degraded and self.pty is not None:
            # Visible, replay-surviving notice (scrollback-backed) — the user
            # must know why the conversation isn't continuing in the TUI. The
            # full chat history still lives in the platform DB/chat view.
            self.pty.inject_output(
                b"\r\n\x1b[2m\xe2\x9a\xa0 This conversation's Codex history "
                b"isn't on this machine \xe2\x80\x94 starting a fresh CLI "
                b"session (chat history is still in the dashboard).\x1b[0m"
                b"\r\n\r\n"
            )

    # --- transcript forwarding (Codex: discover the rollout) -----------------
    def _reset_codex_state(self) -> None:
        """Delete codex's per-machine SQLite state/log DBs in CODEX_HOME (regenerable;
        rollouts in ``sessions/`` are untouched). Mirrors
        ``codex_session.CodexAppServerSession._reset_codex_state`` + the
        ``file_sync._CODEX_RUNTIME_GLOBS`` host-local set. No-op if CODEX_HOME is unset."""
        if self._codex_dir is None:
            return
        for pattern in ("state_*.sqlite*", "logs_*.sqlite*"):
            for path in self._codex_dir.glob(pattern):
                try:
                    path.unlink()
                except OSError as e:
                    logger.warning("codex state reset: couldn't delete %s: %s", path.name, e)

    def _apply_resume_guard(self) -> None:
        """Degrade a resume to a COLD start when the thread's rollout isn't in
        THIS machine's CODEX_HOME. ``codex resume <tid>`` HARD-EXITS (code 1)
        on a missing rollout — e.g. the chat's thread was born on another
        machine or proxy-local (CLI session state is deliberately never
        synced) — and its one-line error drowns in the TUI teardown escapes,
        so the terminal just looks dead. A cold start re-reports the fresh
        thread id to the proxy; a visible notice is injected post-spawn."""
        self._resume_degraded = False
        if self._resume and not self._rollout_on_disk(self._thread_id):
            logger.warning(
                "codex resume: no rollout for thread %s in %s — starting a "
                "fresh session instead",
                self._thread_id,
                (self._codex_dir / "sessions") if self._codex_dir else "?",
            )
            self._resume = False
            self._transcript_skip_existing = False
            self._resume_degraded = True

    def _rollout_on_disk(self, thread_id: str) -> bool:
        """True iff a rollout for ``thread_id`` exists under this CODEX_HOME —
        the precondition for ``codex resume <tid>``, which HARD-EXITS when the
        rollout is missing (same by-name suffix ``_locate_transcript_file``
        uses for the resume pin)."""
        if not thread_id or self._codex_dir is None:
            return False
        sessions_dir = self._codex_dir / "sessions"
        if not sessions_dir.is_dir():
            return False
        try:
            return bool(glob.glob(
                str(sessions_dir / "**" / f"rollout-*-{thread_id}.jsonl"),
                recursive=True,
            ))
        except OSError:
            return False

    def _locate_transcript_file(self) -> "Path | None":
        """Discover THIS session's rollout JSONL under ``CODEX_HOME`` — satellite
        twin of ``codex_rollout_tailer.resolve_rollout_path``. The rollout is named
        ``rollout-<ts>-<thread_uuid>.jsonl`` (NOT by our session id), and CODEX_HOME
        is per-(user, agent) scope, so on resume we prefer the thread's own rollout;
        otherwise the newest ``codex-tui`` rollout for this cwd created after spawn,
        never one already pinned by a sibling live session."""
        if self._codex_dir is None:
            return None
        sessions_dir = self._codex_dir / "sessions"
        if not sessions_dir.is_dir():
            return None
        try:
            candidates = glob.glob(
                str(sessions_dir / "**" / "rollout-*.jsonl"), recursive=True
            )
        except OSError:
            return None
        cwd_str = str(self._cwd) if self._cwd else ""
        others = {p for p in _pinned_rollouts if p != self._transcript_pin}

        # Resume: the thread's own rollout (codex resume <tid> appends to it).
        if self._thread_id:
            suffix = f"-{self._thread_id}.jsonl"
            for path in candidates:
                if path in others or not path.endswith(suffix):
                    continue
                meta = _read_session_meta(path)
                if meta and meta.get("thread_source") != "subagent":
                    return self._pin(path)

        # Fresh (or a resume whose rollout wasn't found by name): the newest
        # codex-tui rollout for this cwd created after spawn.
        best: str | None = None
        best_mtime = -1.0
        for path in candidates:
            if path in others:
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if not self._thread_id and mtime < self._started_at - _RESOLVE_MTIME_MARGIN_S:
                continue
            meta = _read_session_meta(path)
            if not meta:
                continue
            if meta.get("originator") != "codex-tui":
                continue
            if meta.get("thread_source") == "subagent":
                continue
            if cwd_str and not _cwd_match(meta.get("cwd", ""), cwd_str):
                continue
            if mtime > best_mtime:
                best, best_mtime = path, mtime
        if best:
            return self._pin(best)
        return None

    def _pin(self, path: str) -> Path:
        _pinned_rollouts.add(path)
        self._transcript_pin = path
        return Path(path)

    def _on_close(self) -> None:
        if self._transcript_pin is not None:
            _pinned_rollouts.discard(self._transcript_pin)
            self._transcript_pin = None
