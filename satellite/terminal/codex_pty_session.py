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

import asyncio
import contextlib
import errno
import fnmatch
import json
import logging
import os
import re
import sys
import time
from collections.abc import Iterator
from pathlib import Path

from .._vendored import layout, session_text
from .._vendored.codex_approvals import approval_for_sandbox, tui_reviewer_flags
from ..config import (
    otodock_dir,
    session_tools_env,
    write_command_wrappers,
    write_if_changed,
)
from ..host import path_translator, safe_fs
from ..sessions import session_homes
from ..sessions.codex_files import (
    CODEX_TOOLS_TABLE,
    _validate_config_toml,
    chmod_private,
    configured_provider,
    drop_store_leftovers,
    local_provider_toml,
    runs_on_local_model,
    write_or_drop_auth_json,
    write_or_drop_model_catalog,
)
from ..sessions.codex_session import (
    _write_codex_hook_scripts,
    _write_codex_hooks,
    session_text_mode,
    session_text_of,
)
from .pty_session_base import BasePtySession

logger = logging.getLogger("satellite")

# Why a resume started fresh (``_apply_resume_guard``): the rollout is not on
# this machine, or it was recorded with another model provider.
_DEGRADED_MISSING = "missing"
_DEGRADED_PROVIDER = "provider"

# Fresh-spawn lower bound slack: Codex writes its rollout within ~1 s of spawn; allow
# clock/race slack so we never miss it but still exclude a sibling chat's older
# rollout. Mirrors the proxy's codex_rollout_tailer._RESOLVE_MTIME_MARGIN_S.
_RESOLVE_MTIME_MARGIN_S = 5.0

# Rollouts pinned by a live CodexPtySession on this satellite, so two concurrent
# interactive Codex sessions for the SAME (user, agent) — which share one CODEX_HOME
# AND cwd — can't both grab (and double-forward) the same rollout. Mirrors the
# proxy tailer's ``_resolved``/``others`` exclusion.
_pinned_rollouts: set[str] = set()

# What a search of the rollouts answers (the platform's
# ``codex_rollout_tailer.find_rollout``).
_FOUND, _MISSING, _UNDECIDED = "found", "missing", "undecided"
# A thread id names a file: Codex's ids are UUIDs.
_THREAD_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
# How many entries a search reads before it answers "could not tell".
_FIND_MAX_ENTRIES = 100_000
# A rollout sits three folders below ``sessions`` (YYYY/MM/DD): deeper
# folders are not entered.
_FIND_MAX_DEPTH = 8
# A ``session_meta`` record is under 2 KiB; a longer first record reads as
# unknown.
_META_MAX_BYTES = 64 * 1024


def _read_session_meta(root, rel: str) -> dict | None:
    """The first record's ``session_meta`` payload of the rollout at ``rel``
    beneath ``root`` (a regular file with no link on the way, a pipe refused
    at once), read up to ``_META_MAX_BYTES``; None otherwise."""
    try:
        fd, _st = safe_fs.open_regular_for_read(root, rel)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as fh:
            first = fh.readline(_META_MAX_BYTES + 1)
    except OSError:
        return None
    if len(first) > _META_MAX_BYTES:
        return None
    try:
        obj = json.loads(first.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if not isinstance(obj, dict) or obj.get("type") != "session_meta":
        return None
    payload = obj.get("payload")
    return payload if isinstance(payload, dict) else {}


#: A walk cut at its cap that took nothing is not run again for this long:
#: the poll runs on the event loop every 0.8 s.
_CAPPED_RETRY_S = 60.0


def _walk_rollouts(root, base: str, unread: list[OSError],
                   linked: list[str] | None = None) -> Iterator[tuple[safe_fs.Root, str, str]]:
    """``(folder, folder_rel, name)`` for each ``rollout-*.jsonl`` regular
    file beneath ``base``: the newest dated folders first, none entered
    through a link or more than ``_FIND_MAX_DEPTH`` below ``base``; the
    ``folder`` handle is valid until the walk moves on. A missing ``base`` or
    a link at it or above it yields nothing; a folder that could not be read
    goes to ``unread``, and a link met on the way to ``linked`` when given;
    past ``_FIND_MAX_ENTRIES`` entries, in all or in one folder, raises
    ``TooManyEntries``."""
    seen = 0
    pending = [(base, 0)]
    while pending:
        rel, depth = pending.pop()
        with contextlib.ExitStack() as held:
            try:
                folder = held.enter_context(safe_fs.open_root(root, rel))
                listing = safe_fs.list_beneath(folder, max_entries=_FIND_MAX_ENTRIES)
            except safe_fs.TooManyEntries:
                raise
            except safe_fs.SymlinkRefused:
                if linked is not None:
                    linked.append(rel)
                continue
            except (FileNotFoundError, NotADirectoryError):
                continue  # missing, or gone since its parent's listing
            except OSError as exc:
                if rel != base or not isinstance(exc, safe_fs.SafeFsError):
                    unread.append(exc)
                continue
            if linked is not None:
                linked.extend(f"{rel}/{name}" for name in listing.links)
            seen += len(listing.dirs) + len(listing.files) + len(listing.links) + len(listing.other)
            if seen > _FIND_MAX_ENTRIES:
                raise safe_fs.TooManyEntries(errno.EFBIG, "more entries than the search's cap", base)
            for name in listing.files:
                if fnmatch.fnmatchcase(name, "rollout-*.jsonl"):
                    yield folder, rel, name
            if depth < _FIND_MAX_DEPTH:
                # Popped last-in first: the newest name (YYYY, MM, DD) first.
                pending.extend((f"{rel}/{d}", depth + 1) for d in listing.dirs)


def _build_codex_config_toml(
    trusted_cwd: str, mcp_toml: str, local_provider: dict | None = None,
    codex_dir: Path | None = None, catalog_dir: Path | None = None,
) -> str:
    """Build the interactive `codex` config.toml — satellite twin of the proxy's
    ``core.layers.codex.layer._write_config_toml(interactive=True, trusted_cwd=…)``.

    The proxy ships ONLY the ``[mcp_servers.*]`` sections (``mcp_registry._servers_to_toml``);
    the interactive HEADER is added here so a remote Codex TUI matches local:
      - ``project_doc_max_bytes`` — large AGENTS.md system prompts (default cap 32 KB);
      - ``check_for_update_on_startup`` off — the TUI's update prompt would break the
        version pin (`npm install -g` on Enter); ``suppress_unstable_features_warning``
        pairs with the request_user_input feature flag below;
      - ``model_provider = "oto_local"`` + ``[model_providers.oto_local]`` — ONLY when
        the start payload carries ``local_model_provider`` (a local OpenAI-compatible
        endpoint; see ``local_provider_toml``; with a ``catalog_json`` also the root
        ``model_catalog_json`` key pointing at ``<codex_dir>/models.json``). The root
        keys sit with the other root keys, the table after ``[projects]``;
      - ``[memories]`` off — the otodock memory system is the single source of truth;
      - ``[features] plugins = false`` — skip Codex's curated-plugins startup sync (lean-start);
      - ``[features] hooks = true`` — run the PreToolUse ``permission_gate`` FLOOR (the
        command-level deny floor; WITHOUT this the floor is silently off on remote!);
      - ``[projects."<cwd>"] trust_level = "trusted"`` — skip Codex's "Do you trust the
        contents of this directory?" startup prompt. Keyed by the SATELLITE-absolute cwd
        (the satellite owns it — no path translation needed), under BOTH separator forms
        (Windows codex may normalise to forward slashes; the set collapses to one on Unix).
    KEEP IN SYNC with the proxy's ``_write_config_toml``. ``catalog_dir`` is
    the folder the engine reads (a session's home), where its model catalog
    is; the worktrees stay beside ``codex_dir``, the store.
    """
    root_line, provider_table = local_provider_toml(local_provider, catalog_dir or codex_dir)
    parts = [
        "project_doc_max_bytes = 300000",
        # Root keys stay above the first [table] header. check_for_update
        # guards the pin (the TUI's update prompt runs `npm install -g` on
        # Enter); the suppress key silences the "under-development features"
        # warning our request_user_input flag would otherwise print.
        "check_for_update_on_startup = false",
        "suppress_unstable_features_warning = true",
    ]
    if root_line:
        parts.append(root_line)
    parts += [
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
        # Codex 0.152.0 made update_plan opt-in — keep the plan tool on
        # (twin of the proxy's _CODEX_TOOLS_TABLE and the headless writer).
        CODEX_TOOLS_TABLE,
    ]
    if trusted_cwd:
        for key in {trusted_cwd, trusted_cwd.replace("\\", "/")}:
            esc = key.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'[projects."{esc}"]')
            parts.append('trust_level = "trusted"')
    if codex_dir is not None:
        # Codex 0.162's managed worktrees (the TUI's create_worktree and
        # list_worktrees tools) check out under $CODEX_HOME/worktrees by
        # default: the .codex dir the proxy's path policy protects and whose
        # children the sync manifest does not prune. The hidden sibling at the
        # scope root is neither protected nor synced. Satellite-absolute, as
        # Codex requires (twin of the proxy's [desktop] table).
        root = os.path.abspath(str(Path(codex_dir).parent / ".codex-worktrees"))
        esc_root = root.replace("\\", "\\\\").replace('"', '\\"')
        parts.append("[desktop]")
        parts.append(f'git-worktree-root = "{esc_root}"')
    # No model picker in place of the composer near a usage limit: text the
    # platform types would choose in it (twin of the proxy's writer).
    parts += ["[notice]", "hide_rate_limit_model_nudge = true"]
    if provider_table:
        parts.append(provider_table)
    if mcp_toml.strip():
        # The remote ``startup_timeout_sec`` floor (and per-MCP overrides for
        # heavy servers like google-workspace) is applied PROXY-side in
        # ``remote_mcp_rewrite._rewrite_mcp_toml_for_remote`` — which covers this
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
    #: Until when a rollout search that was cut at its cap and took nothing waits.
    _capped_until = 0.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._codex_dir: Path | None = None
        # The folder the engine reads: the session's home when the start asks
        # for one, else the store (``credentials_update`` writes a pushed
        # sign-in there).
        self._engine_dir: Path | None = None
        self._started_at = 0.0  # spawn time → excludes a sibling chat's older rollout
        self._thread_id = (self.config.get("thread_id") or "").strip()
        # Resume iff the proxy said so AND a thread id is known (codex resume <tid>).
        self._resume = bool(self.config.get("resume")) and bool(self._thread_id)
        # On resume the rollout already holds the prior conversation (already in the
        # proxy DB) → skip past it on first locate and forward only new lines.
        self._transcript_skip_existing = self._resume
        self._transcript_pin: str | None = None
        # The platform gives the session its own profile (``session_text``).
        self._own_text = False

    async def _start_compute(self) -> None:
        """A Codex terminal on a compute machine: the platform wrote the
        ``config.toml``, the session's profile and login file into the shared folder
        and planned the TUI's argv (``resume <thread>`` or the flags with the
        cold prompt); this machine renders the sandbox and spawns the plan.
        The rollout is found by the sandbox's cwd the plan names; no state
        reset of its own (the platform's spawn runs the same files)."""
        from ..sessions import credential_exit, sandbox_host, ssh_agent
        self._own_copy = await sandbox_host.ready_own_copy(self.config, self.sat_config,
                                                           self.session_id, self.agent_dir)
        self._codex_dir = self.agent_dir / self.config["codex_dir_relative"]
        self._engine_dir = session_homes.home_path(self._codex_dir, self.session_id,
                                                   self.config.get("session_home"))
        if session_text_mode(self.config):
            # An own copy may still hold an AGENTS.md from before the
            # session's own text; the plan's argv names the session's profile.
            session_text.remove_shared_text(self._codex_dir)
            if not session_text.profile_path(self._engine_dir, self.session_id).is_file():
                logger.error("Codex compute terminal %s: its own profile is missing", self.session_id[:8])
                raise RuntimeError("the session's own profile is missing")
        self._apply_resume_guard()
        # The session's SSH agent socket before the render, which binds it;
        # the credential exit's door before it too (the render binds the bundle).
        agent_env = await asyncio.to_thread(ssh_agent.start, self.session_id, self.config)
        await asyncio.to_thread(credential_exit.start, self.session_id, self.config)
        cmd = await sandbox_host.spawn_command(self.config, self.sat_config, "codex", for_pty=True)
        if self._resume_degraded and cmd and "resume" in cmd:
            # The plan resumes a thread this folder cannot resume: the flags
            # alone, as the platform's own degraded start spawns.
            i = cmd.index("resume")
            cmd = cmd[:i] + cmd[i + 2:]
        env = sandbox_host.spawn_env(self.config)
        ssh_agent.merge_env(env, agent_env, self.config)
        self._started_at = time.time()
        await self._wire_and_spawn(cmd, env, None)
        self._say_degraded()

    def _say_degraded(self) -> None:
        if self._resume_degraded and self.pty is not None:
            # Visible, replay-surviving notice (scrollback-backed) — the user
            # must know why the conversation isn't continuing in the TUI. The
            # full chat history still lives in the platform DB/chat view.
            why = (b"was recorded with another model provider, so a fresh CLI "
                   b"session starts" if self._resume_degraded == _DEGRADED_PROVIDER
                   else b"isn't on this machine \xe2\x80\x94 starting a fresh CLI "
                        b"session")
            self.pty.inject_output(
                b"\r\n\x1b[2m\xe2\x9a\xa0 This conversation's Codex history " + why
                + b" (chat history is still in the dashboard).\x1b[0m\r\n\r\n"
            )

    async def start(self) -> None:
        """Write the .codex config tree + spawn the interactive Codex TUI under a PTY."""
        if self._compute:
            await self._start_compute()
            return
        # cwd may be an arbitrary absolute folder (otodock-CLI); the .codex
        # dir stays agent_dir-rooted via codex_dir_relative regardless.
        self._cwd = self._resolve_work_cwd()
        self._codex_dir = self.agent_dir / self.config["codex_dir_relative"]
        self._codex_dir.mkdir(parents=True, exist_ok=True)
        # The session's home when the start asks for one (its settings, hooks,
        # model catalog, profile and sign-in); the rollouts the guard reads,
        # the scripts and the databases stay the store's.
        self._engine_dir = session_homes.open_home(self._codex_dir, self.session_id,
                                                   self.config.get("session_home"))
        in_home = self._engine_dir != self._codex_dir
        if in_home:
            drop_store_leftovers(self.agent_dir, self.config["codex_dir_relative"])
        self._apply_resume_guard()

        # --- write the .codex/ config tree (mirror CodexSession.start) --------
        _write_codex_hook_scripts(self._codex_dir, self.config.get("hook_scripts", {}))
        write_command_wrappers(self._codex_dir, self.config.get("command_wrappers", {}))
        self._own_text = session_text_mode(self.config)
        if self._own_text:
            # The session's own profile (``-p <session>``, its text as
            # developer_instructions), never a file every session of the
            # home reads.
            session_text.remove_shared_text(self._codex_dir)
            session_text.write_session_files(
                self._engine_dir, self.session_id, text=session_text_of(self.config),
                config={}, profile="", writer=write_if_changed)
        else:
            (self._codex_dir / "AGENTS.md").write_text(session_text_of(self.config))
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
                username=layout.user_of(
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
        local_provider = self.config.get("local_model_provider")
        config_toml_text = _build_codex_config_toml(
            str(self._cwd), toml_content,
            local_provider=local_provider, codex_dir=self._codex_dir, catalog_dir=self._engine_dir,
        )
        _validate_config_toml(config_toml_text, self._engine_dir / "config.toml")
        write_or_drop_model_catalog(self._engine_dir, local_provider)
        (self._engine_dir / "config.toml").write_text(config_toml_text)
        chmod_private(self._engine_dir / "config.toml")
        write_or_drop_auth_json(self._engine_dir, self.config.get("auth_json"))
        _write_codex_hooks(self._engine_dir, session_text_hook=self._own_text,
                           start_context=bool(self.config.get("start_context_parts")),
                           scripts_dir=self._codex_dir)

        # --- env (mirror CodexSession + the bare-TUI interactive vars) --------
        # The shared base set OTO_SESSION_ID + UTF-8 + TERM. Add CODEX_HOME and the
        # two interactive flags the local codex layer sets: OTO_INTERACTIVE (the
        # PostToolUse forwarder no-ops; decide_tool_permission returns "ask" for
        # the ask-tier — the deny-only gate stays silent on it, so Codex's own
        # -a on-request prompts), and OTO_HOOK_DENY_ONLY
        # (Codex's PreToolUse hook rejects permissionDecision:"allow", so the gate
        # emits JSON only to DENY; the hard-deny FLOOR still enforces).
        env = self._base_env()
        env.update(await self._session_agent_env())
        env["CODEX_HOME"] = str(self._engine_dir)
        if in_home:
            # The databases stay the store's, as the rollouts do (its link).
            env["CODEX_SQLITE_HOME"] = str(self._codex_dir)
        env["OTO_INTERACTIVE"] = "1"
        env["OTO_HOOK_DENY_ONLY"] = "1"
        # Interpreter for the Windows .cmd hook wrappers (codex_hook_command)
        # — the batch text stays ASCII by resolving python through the env.
        env["OTO_HOOK_PY"] = sys.executable
        env = session_tools_env(self._translate_env(env), self._codex_dir)

        # --- build the INTERACTIVE command ------------------------------------
        from ..host.cli_versions import resolve_spawn_bin_async
        codex_bin = await resolve_spawn_bin_async(
            "codex", self.sat_config.bin_hint("codex"), for_pty=True,
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
            # Codex 0.157 auto-starts a shared background server (its own Codex
            # install, outside the pin) for eligible TUIs; the trust flag above
            # already keeps this one embedded, and --no-daemon turns the
            # auto-start off with the "Running without the shared background
            # server" line it printed at start (mirrors the local layer; the
            # flag exists on 0.156.1 too).
            "--no-daemon",
            "-C", str(self._cwd),
        ]
        model = self.config.get("model", "")
        if model:
            flags += ["-m", model]
        # Auto (a 1.9 proxy's ``autoReview``) starts the terminal with
        # Codex's own reviewer; every other mode keeps its own prompt.
        flags += tui_reviewer_flags(self.config.get("permission_mode") or "")
        effort = self.config.get("effort", "")  # already codex-mapped by the proxy
        if effort:
            flags += ["-c", f'model_reasoning_effort="{effort}"']
        if self._own_text:
            flags += ["-p", session_text.checked_name(self.session_id)]
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
        from ..sessions import sandbox_host
        await self._wire_and_spawn(sandbox_host.paired_command(cmd), env, str(self._cwd))
        self._say_degraded()

    # --- transcript forwarding (Codex: discover the rollout) -----------------
    def _apply_resume_guard(self) -> None:
        """Degrade a resume to a COLD start when the thread's rollout isn't in
        THIS machine's CODEX_HOME. ``codex resume <tid>`` HARD-EXITS (code 1)
        on a missing rollout — e.g. the chat's thread was born on another
        machine or proxy-local (CLI session state is deliberately never
        synced) — and its one-line error drowns in the TUI teardown escapes,
        so the terminal just looks dead. A cold start re-reports the fresh
        thread id to the proxy; a visible notice is injected post-spawn.
        A thread recorded with another model provider (a chat moved between
        a local model and OpenAI while no session ran) gets the same cold
        start. It is the only check on a terminal's provider: from 0.168
        ``codex resume <tid>`` resumes on the recorded provider, whatever
        ``config.toml`` names, with no notice (0.166 exited, 0.167 parked on
        a notice). A rollout that names no provider resumes on its old one."""
        self._resume_degraded = ""
        if not self._resume:
            return
        # One search; one that could not tell keeps the resume (logged there).
        found, rel = self._find_rollout(self._thread_id)
        if found == _MISSING:
            logger.warning(
                "codex resume: no rollout for thread %s in %s — starting a "
                "fresh session instead",
                self._thread_id,
                (self._codex_dir / "sessions") if self._codex_dir else "?",
            )
            self._resume_degraded = _DEGRADED_MISSING
        elif rel is not None:
            recorded = self._rollout_provider(rel)
            current = configured_provider(runs_on_local_model(self.config))
            if recorded and recorded != current:
                logger.warning("codex resume: thread %s was recorded with provider %s, this "
                               "spawn writes %s; starting a fresh session instead",
                               self._thread_id, recorded, current)
                self._resume_degraded = _DEGRADED_PROVIDER
        if self._resume_degraded:
            self._resume = False
            self._transcript_skip_existing = False

    def _rollout_provider(self, rel: str) -> str:
        """The model provider the rollout at ``rel`` (beneath the agent's
        folder) recorded in its first line, ``session_meta``, or "" when it
        cannot be read."""
        meta = _read_session_meta(self.agent_dir, rel)
        provider = meta.get("model_provider") if meta else None
        return provider if isinstance(provider, str) else ""

    def _sessions_rel(self) -> str | None:
        """CODEX_HOME's ``sessions`` as a path beneath the agent's folder."""
        if self._codex_dir is None:
            return None
        try:
            return f"{self._codex_dir.relative_to(self.agent_dir).as_posix()}/sessions"
        except ValueError:
            return None

    def _find_rollout(self, thread_id: str) -> tuple[str, str | None]:
        """Where the rollout of ``thread_id`` is beneath the agent's folder:
        ``(_FOUND, rel)``, ``(_MISSING, None)`` or ``(_UNDECIDED, None)``. The
        first ``rollout-*-<thread>.jsonl`` regular file of ``_walk_rollouts``;
        an id that cannot name a file, a missing folder or a link at
        ``sessions`` or above it is MISSING; a search past its cap or one that
        met a folder it could not read could not tell."""
        base = self._sessions_rel()
        if base is None or not _THREAD_RE.fullmatch(thread_id or ""):
            return _MISSING, None
        pattern = f"rollout-*-{thread_id}.jsonl"
        unread: list[OSError] = []
        try:
            with safe_fs.open_root(self.agent_dir) as root:
                for _folder, rel, name in _walk_rollouts(root, base, unread):
                    if fnmatch.fnmatchcase(name, pattern):
                        return _FOUND, f"{rel}/{name}"
        except safe_fs.TooManyEntries:
            logger.warning("codex rollout of thread %s: the search passed %d entries in %s",
                           thread_id[:8], _FIND_MAX_ENTRIES, base)
            return _UNDECIDED, None
        except (FileNotFoundError, NotADirectoryError):
            return _MISSING, None
        except OSError:
            logger.warning("codex rollout of thread %s: %s could not be searched", thread_id[:8], base,
                           exc_info=True)
            return _UNDECIDED, None
        if unread:
            logger.warning("codex rollout of thread %s: %d folder(s) of %s could not be read",
                           thread_id[:8], len(unread), base)
            return _UNDECIDED, None
        return _MISSING, None

    def _rollout_on_disk(self, thread_id: str) -> bool:
        """Whether a rollout for ``thread_id`` may be under this CODEX_HOME —
        the precondition for ``codex resume <tid>``, which HARD-EXITS when the
        rollout is missing. A search that could not tell answers True: the
        resume stays."""
        return self._find_rollout(thread_id)[0] != _MISSING

    def _locate_transcript_file(self) -> str | None:
        """Discover THIS session's rollout JSONL under ``CODEX_HOME``, as a path
        beneath the agent's folder — satellite twin of
        ``codex_rollout_tailer.resolve_rollout_path``. The rollout is named
        ``rollout-<ts>-<thread_uuid>.jsonl`` (NOT by our session id), and CODEX_HOME
        is per-(user, agent) scope, so on resume we prefer the thread's own rollout;
        otherwise the newest ``codex-tui`` rollout for this cwd created after spawn,
        never one already pinned by a sibling live session. One bounded walk
        (``_walk_rollouts``): a folder it could not read is passed over. A
        walk cut at its cap keeps the newest fresh rollout it met, takes no
        other session's for a resume, and when it took nothing the next walk
        waits ``_CAPPED_RETRY_S``."""
        if time.monotonic() < self._capped_until:
            return None
        base = self._sessions_rel()
        if base is None:
            return None
        # A compute session's rollout recorded the sandbox's folder.
        cwd_str = (str(self.config.get("cwd") or "") if self._compute
                   else str(self._cwd) if self._cwd else "")
        others = {p for p in _pinned_rollouts if p != self._transcript_pin}
        # Resume: the thread's own rollout (codex resume <tid> appends to it).
        # A resume the guard made a cold start writes a new rollout: the old
        # one holds history the chat already shows.
        own = (f"rollout-*-{self._thread_id}.jsonl"
               if self._resume and _THREAD_RE.fullmatch(self._thread_id or "") else "")
        best: str | None = None
        best_mtime = -1.0
        later: list[str] = []
        unread: list[OSError] = []
        linked: list[str] = []
        capped = False
        try:
            with safe_fs.open_root(self.agent_dir) as root:
                try:
                    for folder, rel, name in _walk_rollouts(root, base, unread, linked):
                        path = f"{rel}/{name}"
                        if self._pin_key(path) in others:
                            continue
                        if self._resume:
                            if own and fnmatch.fnmatchcase(name, own):
                                meta = _read_session_meta(folder, name)
                                if meta and meta.get("thread_source") != "subagent":
                                    return self._pin(path)
                            later.append(path)
                            continue
                        mtime = self._fresh_rollout(folder, name, cwd_str)
                        if mtime is not None and mtime > best_mtime:
                            best, best_mtime = path, mtime
                except safe_fs.TooManyEntries:
                    capped = True
                    if own:
                        self._warn_once("cap", "Codex PTY %s: the rollout search stopped at %d entries in %s "
                                        "before it met this terminal's own rollout: its history does not "
                                        "reach the chat", self.session_id[:8], _FIND_MAX_ENTRIES, base)
                    else:
                        # The newest folders come first: a new rollout is
                        # met before the cap.
                        self._warn_once("cap", "Codex PTY %s: the rollout search stopped at %d entries in %s; "
                                        "the newest rollouts it met are used",
                                        self.session_id[:8], _FIND_MAX_ENTRIES, base)
                # A resume whose rollout wasn't found by name: the newest
                # codex-tui rollout for this cwd, at any time. A walk cut
                # short never met that rollout, and the newest one it met is
                # another session's.
                if not (capped and own):
                    for path in later:
                        mtime = self._fresh_rollout(root, path, cwd_str)
                        if mtime is not None and mtime > best_mtime:
                            best, best_mtime = path, mtime
        except OSError:
            logger.debug("Codex PTY %s: rollout search failed", self.session_id[:8], exc_info=True)
            return None
        if linked:
            self._warn_once("link", "Codex PTY %s: %s is a link, which the terminal does not follow: "
                            "rollouts there do not reach the chat", self.session_id[:8], linked[0])
        if best:
            return self._pin(best)
        if capped:
            self._capped_until = time.monotonic() + _CAPPED_RETRY_S
        return None

    def _fresh_rollout(self, where, rel: str, cwd_str: str) -> float | None:
        """The modification time of the rollout at ``rel`` beneath ``where``
        when it is a ``codex-tui`` rollout of this cwd (on a fresh start, one
        written since the spawn), else None."""
        try:
            st = safe_fs.lstat_beneath(where, rel)
        except OSError:
            return None
        if not self._resume and st.st_mtime < self._started_at - _RESOLVE_MTIME_MARGIN_S:
            return None
        meta = _read_session_meta(where, rel)
        if not meta or meta.get("originator") != "codex-tui" or meta.get("thread_source") == "subagent":
            return None
        if cwd_str and not _cwd_match(meta.get("cwd", ""), cwd_str):
            return None
        return st.st_mtime

    def _pin_key(self, rel: str) -> str:
        """The rollout's identity in ``_pinned_rollouts``: its path as text."""
        return str(self.agent_dir.joinpath(*rel.split("/")))

    def _pin(self, rel: str) -> str:
        key = self._pin_key(rel)
        _pinned_rollouts.add(key)
        self._transcript_pin = key
        return rel

    async def close(self) -> None:
        try:
            await super().close()
        finally:
            # The session's home goes once its process is gone (whatever the
            # close above raised), unless a later open of the same id
            # replaced this terminal (the home is its now).
            if not self._compute and not self.superseded and self._codex_dir is not None:
                session_homes.close_home(self._codex_dir, self.session_id, self.config.get("session_home"))

    def _on_close(self) -> None:
        if self._transcript_pin is not None:
            _pinned_rollouts.discard(self._transcript_pin)
            self._transcript_pin = None
