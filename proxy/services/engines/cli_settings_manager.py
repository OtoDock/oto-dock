"""Interactive-CLI config seeding.

The native Claude TUI (interactive sessions) needs its per-session
``CLAUDE_CONFIG_DIR/.claude.json`` pre-seeded past first-run onboarding —
headless ``-p`` sessions never hit the wizard, so nothing else needs this.
Headless session settings (hooks, deny list, sandbox-off) are written
per-session by ``core/layers/cli/config_dir.build_settings``.
"""

from __future__ import annotations

import json
import logging
import posixpath

logger = logging.getLogger(__name__)

# The CLI's own config file: a few KB. Older builds kept per-project data
# here, so a long-lived store can grow; past this it is left untouched.
_SEED_MAX_BYTES = 64 * 1024 * 1024


def seed_interactive_cli_config(
    host_claude_dir: str, sandbox_cwd: str, theme: str = "dark",
) -> None:
    """Pre-seed ``.claude.json`` so the interactive TUI launches PAST first-run.

    Headless ``-p`` never runs Claude Code's onboarding wizard, so the platform
    never needed this. The interactive TUI does: the sandbox gives each spawn a
    fresh ephemeral ``HOME`` and a minimal ``CLAUDE_CONFIG_DIR/.claude.json``
    (only ``userID``/``firstStartTime`` from headless), so Claude finds no
    "onboarding complete" state and runs theme-picker → login → per-folder trust
    on EVERY launch. Auth itself is fine — the subscription token sits in
    ``CLAUDE_CONFIG_DIR/.credentials.json`` (same as headless) and Claude reads
    it — but the wizard masks it.

    This merges the skip-the-wizard flags into the existing config dir
    (``CLAUDE_CONFIG_DIR/.claude.json`` — the file Claude actually reads when
    ``CLAUDE_CONFIG_DIR`` is set), preserving ``userID``/``projects``/etc.:
      * ``hasCompletedOnboarding`` — skip theme-picker + login wizard;
      * ``theme`` — pre-select (matched to the dashboard light/dark mode);
      * ``projects[cwd].hasTrustDialogAccepted`` — skip "trust this folder?".
    Idempotent; written BEFORE the spawn so Claude reads it at startup (after
    which Claude owns the file). Local interactive sessions only.

    The folder is written by every session of the scope, so the file is read
    and written beneath the agent's real folder (``core.agent_folder``) with
    no link at any part below it: a link, a
    pipe or a device at the name reads as empty and the write replaces it
    with a regular file; a file over ``_SEED_MAX_BYTES`` is left as it is and
    nothing is seeded (an honest file is never cut). Blocking: off the loop.
    """
    if not host_claude_dir:
        return
    from core.agent_folder import beneath_agent
    from services.infra import safe_fs
    try:
        root, base = beneath_agent(host_claude_dir)
    except safe_fs.SafeFsError:
        logger.warning("seed_interactive_cli_config: %s is in no agent's folder", host_claude_dir)
        return
    rel = posixpath.join(base, ".claude.json")
    try:
        data = json.loads(safe_fs.read_bytes_beneath(root, rel, max_size=_SEED_MAX_BYTES))
    except safe_fs.FileTooLarge:
        logger.warning("seed_interactive_cli_config: %s/.claude.json is over %d bytes; not seeded",
                       host_claude_dir, _SEED_MAX_BYTES)
        return
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    data["hasCompletedOnboarding"] = True
    # Fresh config dir per spawn → the CLI's once-per-install notices would
    # re-trigger every session. The auto-mode entry banner is the noisy one
    # (a full paragraph at every auto-mode session start) — mark it seen.
    data["hasSeenAutoModeEntryWarning"] = True
    # Claude Code ≥2.1.x shows a resume-from-summary picker when resuming a
    # session that is old + large ("This session is 2d 17h old and 490.8k
    # tokens… ❯ 1. Resume from summary (recommended)"). Fatal for platform
    # spawns: the cold-flushed first prompt types into the PICKER (lost), and
    # the deferred cold Enter would CONFIRM option 1 — silently degrading the
    # whole session to a summary. This is the key the picker's own
    # "3. Don't ask me again" persists (verified empirically on 2.1.220;
    # changelog-reviewed to 2.1.243 — no picker/key change);
    # seeding it resumes the full session as-is, which is the platform's
    # resume contract. 2.1.304's "Resume this conversation?" question (a Pro
    # or Max login, a large conversation idle past its cache: "Resume" or
    # "Start a new conversation", held over the first prompt sent) checks the
    # same key first, so it never stands between the cold prompt and the
    # resumed session either (read in the 2.1.312 binary).
    data["resumeReturnDismissed"] = True
    # Map the dashboard mode to a Claude theme; default dark. Claude's TUI text
    # color follows this, so it MUST match the xterm background or text is
    # unreadable (a light xterm bg with Claude's dark theme = invisible text).
    data["theme"] = "light" if str(theme).lower().startswith("light") else "dark"
    if sandbox_cwd:
        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            projects = data["projects"] = {}
        proj = projects.setdefault(sandbox_cwd, {})
        if not isinstance(proj, dict):
            proj = projects[sandbox_cwd] = {}
        proj["hasTrustDialogAccepted"] = True
        proj["hasCompletedProjectOnboarding"] = True
        proj.setdefault("projectOnboardingSeenCount", 1)
    try:
        safe_fs.atomic_write_beneath(root, rel, (json.dumps(data, indent=2) + "\n").encode(),
                                     mkdirs=True, fsync=False)
    except OSError:
        logger.warning("seed_interactive_cli_config: could not write %s/.claude.json", host_claude_dir,
                       exc_info=True)
