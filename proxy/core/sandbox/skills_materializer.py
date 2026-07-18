"""Materialize on-demand Agent Skills into a session CLI config dir.

The platform is the only skill SOURCE; the CLIs are where skills RUN.
Enabled ``loading: on_demand`` skills are
projected as standard skill folders into ``<config_dir>/skills/`` —
``$CLAUDE_CONFIG_DIR/skills`` / ``$CODEX_HOME/skills`` — where the CLI's own
progressive disclosure indexes them (name+description in its prompt section,
body read on activation). ``always`` skills never come through here; they are
inlined by the prompt builder.

Reconciliation protocol (audit-hardened, plan §2):

- **Identical set everywhere** — the enabled set comes from
  ``get_on_demand_skills_for_materialization`` (context-free,
  placement-free), so concurrent ensures for the same dir can't ping-pong.
- **Serialized per dir** — ``flock`` on ``skills/.oto-lock`` (ensures run in
  threads across several builders, and multiple proxy workers may race).
- **Stage → digest → atomic swap** — each skill is built in a dot-prefixed
  staging dir, content-digested, and only swapped in (rename) when it
  differs from what's on disk. A live CLI mid-read sees the old or the new
  folder, never a torn one. Digesting the MATERIALIZED tree every ensure —
  not trusting a provenance marker — is what repairs in-place tampering:
  the ``.claude``/``.codex`` tree is RW-bound in the sandbox, so an agent
  can edit its own skill files; whatever it writes is reverted at the next
  session start. This replaces the old ``Skill``-tool denial as the
  "no parallel memory path" guarantee.
- **Quarantine, don't delete** — folders not in the enabled set are moved
  aside to ``skills/.quarantine`` with a loud log (released installs may
  carry agent-written ``skills/`` content from before this system existed).
  Dot-prefixed entries are never touched — ``.system`` is Codex's own
  vendored builtins.
- **Fail-soft per skill** — a copy failure skips that skill and the session
  proceeds; skills must never fail a phone call or scheduled task.

Frontmatter passing through here is SCRUBBED to the declarative whitelist
(``skill_format.scrub_frontmatter``): Claude Code honors ``allowed-tools``
from skill frontmatter, which would pre-authorize tools past the platform's
ask-tier on interactive sessions.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
from pathlib import Path

import yaml

logger = logging.getLogger("claude-proxy.sandbox")

_LOCK_NAME = ".oto-lock"
_MARKER_NAME = ".oto-skill.json"
_QUARANTINE_DIR = ".quarantine"
_QUARANTINE_KEEP = 5


def _skill_tree_digest(root: Path) -> str:
    """sha256 over (relpath, bytes) of every regular file under ``root``.

    The provenance marker is excluded (it records the digest), symlinks are
    ignored (staging never creates them; one appearing IS a divergence — it
    changes nothing in the digest, but the swap that follows replaces it).
    """
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.name == _MARKER_NAME or not p.is_file() or p.is_symlink():
            continue
        h.update(str(p.relative_to(root)).encode())
        h.update(b"\0")
        h.update(p.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def _stage_skill(source: Path, skill_id: str, description: str,
                 staging: Path) -> None:
    """Build the transformed skill folder at ``staging``.

    Two source shapes:
    - ``…/<id>/SKILL.md`` — a standard skill folder: copy the whole folder
      (no symlinks), scrubbing the SKILL.md frontmatter to the whitelist.
    - ``…/<file>.md`` — a legacy flat skill file: synthesize a standard
      folder around it (generated ``name``/``description`` frontmatter from
      the manifest — the CLI needs both to index the skill).
    """
    from services.mcp.skill_format import scrub_frontmatter

    if source.name == "SKILL.md":
        shutil.copytree(source.parent, staging, symlinks=False,
                        ignore=shutil.ignore_patterns(_MARKER_NAME))
        # Defensive sweep: copytree(symlinks=False) follows links; refuse
        # any that survived as links (e.g. dangling ones copied by a future
        # copytree behavior change).
        for p in staging.rglob("*"):
            if p.is_symlink():
                p.unlink()
        skill_md = staging / "SKILL.md"
        skill_md.write_text(scrub_frontmatter(skill_md.read_text()))
    else:
        staging.mkdir(parents=True)
        fm = yaml.safe_dump(
            {"name": skill_id,
             "description": description or f"Platform skill {skill_id}."},
            sort_keys=False, allow_unicode=True, default_flow_style=False,
        )
        body = scrub_frontmatter(source.read_text())
        (staging / "SKILL.md").write_text(f"---\n{fm}---\n\n{body}")


def _quarantine(skills_dir: Path, entry: Path) -> None:
    qdir = skills_dir / _QUARANTINE_DIR
    qdir.mkdir(exist_ok=True)
    dest = qdir / entry.name
    n = 1
    while dest.exists():
        dest = qdir / f"{entry.name}-{n}"
        n += 1
    entry.rename(dest)
    logger.warning(
        "skills reconcile: quarantined unmanaged entry %s -> %s "
        "(platform-managed dir)",
        entry, dest,
    )
    # Bounded: keep the newest N quarantined entries.
    entries = sorted(qdir.iterdir(), key=lambda p: p.stat().st_mtime,
                     reverse=True)
    for old in entries[_QUARANTINE_KEEP:]:
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            old.unlink(missing_ok=True)


def materialize_skills_for_sandbox(agent_name: str, config_dir: Path) -> None:
    """Reconcile ``<config_dir>/skills/`` to the agent's enabled on-demand set.

    Fail-soft at every level: any error logs and leaves the session start
    unaffected (hooks stay fail-hard; skills never block a session).
    """
    try:
        _materialize_locked(agent_name, Path(config_dir))
    except Exception:
        logger.exception(
            "skills materialization failed for agent=%s dir=%s — session "
            "proceeds without on-demand skills", agent_name, config_dir,
        )


def _materialize_locked(agent_name: str, config_dir: Path) -> None:
    from services.mcp import mcp_registry

    wanted = mcp_registry.get_on_demand_skills_for_materialization(agent_name)
    skills_dir = config_dir / "skills"
    if not wanted and not skills_dir.is_dir():
        return  # nothing to add, nothing to reconcile — don't create churn

    skills_dir.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(skills_dir / _LOCK_NAME, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _reconcile(wanted, skills_dir)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _reconcile(wanted: list[tuple[str, Path, str, str, str]],
               skills_dir: Path) -> None:
    # Clear staging debris from crashed prior runs (we hold the lock; live
    # CLIs never read dot-prefixed dirs).
    for stale in skills_dir.glob(".staging-*"):
        shutil.rmtree(stale, ignore_errors=True)

    materialized: set[str] = set()
    for skill_id, source, pkg, version, description in wanted:
        if not source.is_file():
            logger.warning("skill %s: source %s missing — skipped",
                           skill_id, source)
            continue
        staging = skills_dir / f".staging-{skill_id}-{os.getpid()}"
        try:
            _stage_skill(source, skill_id, description, staging)
            expected = _skill_tree_digest(staging)
            (staging / _MARKER_NAME).write_text(json.dumps(
                {"package": pkg, "version": version, "digest": expected},
                indent=2,
            ) + "\n")
            target = skills_dir / skill_id
            if target.is_dir() and _skill_tree_digest(target) == expected:
                shutil.rmtree(staging)
            else:
                old = skills_dir / f".staging-old-{skill_id}-{os.getpid()}"
                if target.exists():
                    target.rename(old)
                staging.rename(target)
                shutil.rmtree(old, ignore_errors=True)
                logger.info("skill %s materialized (pkg=%s v=%s)",
                            skill_id, pkg, version)
            materialized.add(skill_id)
        except Exception:
            logger.exception("skill %s: materialization failed — skipped",
                             skill_id)
            shutil.rmtree(staging, ignore_errors=True)

    # Reconcile removals: quarantine non-dot entries not in the enabled set
    # (disabled/uninstalled skills, on_demand→always restamps, agent-written
    # strays). Dot-prefixed entries — Codex's .system builtins, the lock,
    # .quarantine itself — are never touched.
    for entry in skills_dir.iterdir():
        if entry.name.startswith(".") or entry.name in materialized:
            continue
        try:
            _quarantine(skills_dir, entry)
        except Exception:
            logger.exception("skills reconcile: failed to quarantine %s", entry)
