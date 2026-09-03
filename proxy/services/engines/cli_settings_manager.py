"""Interactive-CLI config seeding.

The native Claude TUI (interactive sessions) needs its per-session
``CLAUDE_CONFIG_DIR/.claude.json`` pre-seeded past first-run onboarding —
headless ``-p`` sessions never hit the wizard, so nothing else needs this.
Headless session settings (hooks, deny list, sandbox-off) are written
per-session by ``core/sandbox/session_config_dir._build_sandbox_cli_settings``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


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
    """
    if not host_claude_dir:
        return
    cfg = Path(host_claude_dir) / ".claude.json"
    try:
        data = json.loads(cfg.read_text()) if cfg.exists() else {}
    except Exception:
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
    # resume contract.
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
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data, indent=2) + "\n")
    except Exception:
        logger.warning("seed_interactive_cli_config: could not write %s", cfg, exc_info=True)
