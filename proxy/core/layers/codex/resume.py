"""The Codex engine's resume: the chat's rollout files and whether a chat can
resume its thread.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import config as app_config
from core import file_commit, layout, placement
from core.execution_layer import (
    AgentConfig,
    ResumeVerdict,
)
from core.layers.codex.spawn import _CodexSpawn
from storage.pg import run_db

if TYPE_CHECKING:
    pass

logger = logging.getLogger("codex-layer")


class _CodexResume(_CodexSpawn):
    """The chat's resume and rollout files."""

    def chat_session_files(self, home: Path, session_id: str, resume_handle: str) -> list[Path]:
        """The rollout of one chat: ``<home>/.codex/sessions/**/*<thread id>.jsonl``
        (the thread id is the chat's resume handle; the caller keeps the
        id-shape and liveness guards)."""
        if not resume_handle:
            return []
        return list((home / ".codex" / "sessions").rglob(f"*{resume_handle}.jsonl"))

    def iter_session_files(self, home: Path):
        for f in (home / ".codex" / "sessions").rglob("rollout-*.jsonl"):
            yield f, f.stem[-36:]

    def resumes_interactive(self, config: AgentConfig) -> bool:
        """Codex resumes by THREAD id (``config.resume_handle``, read through
        from ``chats.codex_thread_id``), whose rollout persists on disk
        independently of the in-memory app-server session AND of the
        warmup's ``resume`` flag (keyed on the just-closed ``-p`` session,
        so False right after a ``-p`` → terminal switch). Remote: the
        rollout lives on the satellite, out of reach of a local glob — trust
        the handle (the satellite's ``CodexPtySession`` locates
        ``rollout-*-<tid>.jsonl`` and ``codex resume <tid>`` continues it; a
        rollout genuinely missing there starts a fresh thread). Local: the
        handle AND its rollout under this session's ``CODEX_HOME``, else a
        switch back to the terminal would start a fresh codex with no
        context. A non-interactive config keeps the warmup's flag."""
        if not config.interactive:
            return config.resume
        tid = (config.resume_handle or "").strip()
        if not placement.is_local(config.execution_target):
            return bool(tid)
        if not tid:
            return False
        # A search that could not decide (past its bound, a folder it could
        # not read) trusts the handle, as on a machine. Blocking: the caller
        # runs it off the event loop.
        from core.session import codex_rollout_tailer as rollouts
        return rollouts.find_rollout(config.sandbox_host_claude_dir, tid, config.agent_name)[0] != rollouts.MISSING

    async def can_resume_session(
        self, session_id: str, *, agent_name: str = "", username: str = "",
        external_home: str = "",
    ) -> ResumeVerdict:
        """Codex resumability is THREAD-backed.

        The thread + its rollout persist on disk independently of any
        running app-server session, so a restart / idle reap is NOT
        context loss — treating it as one made the warmup stamp a false
        ``resume_failed`` (digest reseed + "fresh session" card) on a chat
        whose ``thread/resume`` then succeeded anyway. The chat's
        ``codex_thread_id`` is resolved and its rollout checked under the
        scope's .codex dir (as the CLI layer checks its session JSONL).
        """
        from storage.database import get_chat_by_session
        chat = await run_db(get_chat_by_session, session_id)
        thread_id = (chat or {}).get("codex_thread_id") or ""
        if not thread_id:
            return ResumeVerdict.FRESH

        from core.session import codex_rollout_tailer as rollouts
        from core.session.session_state import get_session_codex_dir
        rec = get_session_codex_dir(session_id)
        codex_dir = (rec or {}).get("home", "")
        if not codex_dir and agent_name:
            agent_dir = app_config.get_agent_dir(agent_name)
            if external_home:
                codex_dir = str(Path(external_home) / ".codex")
            elif username:
                codex_dir = str(layout.user_dir(agent_dir, username) / ".codex")
            else:
                codex_dir = str(agent_dir / layout.WORKSPACE / ".codex")
        if not codex_dir:
            return ResumeVerdict.FRESH
        # A fresh start replaces the chat's context, so only a definite
        # answer takes it: a search that could not decide is UNKNOWN.
        state, _path = await file_commit.run_in("session-probes", rollouts.find_rollout, codex_dir, thread_id,
                                                agent_name)
        if state == rollouts.FOUND:
            return ResumeVerdict.RESUMABLE
        return ResumeVerdict.UNKNOWN if state == rollouts.UNDECIDED else ResumeVerdict.FRESH
