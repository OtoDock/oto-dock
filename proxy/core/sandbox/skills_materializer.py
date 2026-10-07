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
- **Serialized per dir**: ``flock`` on a lock file under
  ``SESSIONS_DIR/skills-locks/`` (ensures run in threads across several
  builders, and multiple proxy workers may race). The lock lives OUTSIDE
  the config dir: that dir is RW-bound into the sandbox, and a session
  holding a lock there could stall every later session start of its scope.
- **Stage → digest → atomic swap** — each skill is built in a dot-prefixed
  staging dir, content-digested, and only swapped in (rename) when it
  differs from what's on disk. A live CLI mid-read sees the old or the new
  folder, never a torn one. Digesting the MATERIALIZED tree every ensure —
  not trusting a provenance marker — is what repairs in-place tampering:
  the ``.claude``/``.codex`` tree is RW-bound in the sandbox, so an agent
  can edit its own skill files; whatever it writes is reverted at the next
  session start. This replaces the old ``Skill``-tool denial as the
  "no parallel memory path" guarantee.
- **No symlink is ever followed**: the package is copied from beneath
  ``MCPS_DIR`` refusing links (a package that ships one is skipped whole:
  the target's bytes never land in a sandbox-readable folder), and the
  staging writes go through ``safe_fs`` beneath ``AGENTS_DIR`` so a link an
  agent plants in its RW config dir between the copy and the rewrite is
  replaced, never written through.
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
import posixpath
import stat
from pathlib import Path

import yaml

from services.infra import safe_fs

logger = logging.getLogger("claude-proxy.sandbox")

_MARKER_NAME = ".oto-skill.json"
_QUARANTINE_DIR = ".quarantine"
_QUARANTINE_KEEP = 5
_LOCKS_DIRNAME = "skills-locks"
# A skill body is prose; a package file past this is not one.
_MAX_SKILL_BYTES = 8 * 1024 * 1024


def _skill_tree_digest(rel: str) -> str:
    """sha256 over (relpath, bytes) of every regular file beneath ``rel``
    (under ``AGENTS_DIR``), walked and read with no link followed.

    The provenance marker is excluded (it records the digest). A symlink is
    left out: staging never creates one, and one appearing changes nothing
    in the digest while the swap that follows replaces it. A file swapped
    for a link between the listing and the read is refused by the open,
    which the caller counts as a divergence.
    """
    import config

    h = hashlib.sha256()
    prefix = f"{rel}/"
    for step in safe_fs.walk_beneath(config.AGENTS_DIR, rel):
        for name in step.files:
            if name == _MARKER_NAME:
                continue
            sub = step.path(name)
            h.update(sub[len(prefix):].encode())
            h.update(b"\0")
            h.update(safe_fs.read_bytes_beneath(config.AGENTS_DIR, sub, max_size=_MAX_SKILL_BYTES))
            h.update(b"\0")
    return h.hexdigest()


def _entries(rel: str) -> safe_fs.WalkStep:
    """The listing of the directory at ``rel`` beneath ``AGENTS_DIR``, no
    descent."""
    import config

    walk = safe_fs.walk_beneath(config.AGENTS_DIR, rel)
    try:
        return next(walk)
    finally:
        walk.close()


def _present(rel: str) -> bool:
    import config

    try:
        safe_fs.lstat_beneath(config.AGENTS_DIR, rel)
    except FileNotFoundError:
        return False
    return True


def _is_dir(rel: str) -> bool:
    """A directory at ``rel`` itself, never through a link."""
    import config

    try:
        return stat.S_ISDIR(safe_fs.lstat_beneath(config.AGENTS_DIR, rel).st_mode)
    except OSError:
        return False


def _remove_entry(rel: str) -> None:
    """Remove whatever sits at ``rel``: a directory tree, else the file or
    the link itself; nothing there is fine."""
    import config

    if _is_dir(rel):
        safe_fs.rmtree_beneath(config.AGENTS_DIR, rel, missing_ok=True)
    else:
        safe_fs.unlink_beneath(config.AGENTS_DIR, rel, missing_ok=True)


def _stage_skill(source: Path, skill_id: str, description: str,
                 staging_rel: str) -> None:
    """Build the transformed skill folder at ``staging_rel`` (below
    ``AGENTS_DIR``) from ``source`` (below ``MCPS_DIR``).

    Two source shapes:
    - ``…/<id>/SKILL.md`` — a standard skill folder: copy the whole folder
      (a symlink anywhere in it ends the copy and skips the skill),
      scrubbing the SKILL.md frontmatter to the whitelist.
    - ``…/<file>.md`` — a legacy flat skill file: synthesize a standard
      folder around it (generated ``name``/``description`` frontmatter from
      the manifest — the CLI needs both to index the skill).
    """
    import config
    from services.mcp.skill_format import scrub_frontmatter

    src_rel = safe_fs.rel_under(source, config.MCPS_DIR)
    if source.name == "SKILL.md":
        safe_fs.copytree_beneath(
            config.MCPS_DIR, posixpath.dirname(src_rel), config.AGENTS_DIR, staging_rel,
            symlinks="refuse",
            ignore=lambda _rel, names: [n for n in names if n == _MARKER_NAME],
        )
        raw = safe_fs.read_bytes_beneath(
            config.AGENTS_DIR, f"{staging_rel}/SKILL.md", max_size=_MAX_SKILL_BYTES,
        )
        text = scrub_frontmatter(raw.decode("utf-8"), origin=str(source))
        safe_fs.atomic_write_beneath(
            config.AGENTS_DIR, f"{staging_rel}/SKILL.md", text.encode("utf-8"), fsync=False,
        )
    else:
        safe_fs.mkdirs_beneath(config.AGENTS_DIR, staging_rel, exist_ok=False)
        fm = yaml.safe_dump(
            {"name": skill_id,
             "description": description or f"Platform skill {skill_id}."},
            sort_keys=False, allow_unicode=True, default_flow_style=False,
        )
        raw = safe_fs.read_bytes_beneath(config.MCPS_DIR, src_rel, max_size=_MAX_SKILL_BYTES)
        body = scrub_frontmatter(raw.decode("utf-8"), origin=str(source))
        safe_fs.atomic_write_beneath(
            config.AGENTS_DIR, f"{staging_rel}/SKILL.md",
            f"---\n{fm}---\n\n{body}".encode("utf-8"), fsync=False,
        )


def _quarantine(skills_rel: str, name: str) -> None:
    """Move the unmanaged entry ``name`` of the skills dir into its
    ``.quarantine`` sibling, every step beneath ``AGENTS_DIR`` with no link
    followed: the config dir is writable from inside the sandbox, so a link
    planted at the quarantine's name would otherwise redirect the move and
    the prune below into any directory on the host."""
    import config

    root = config.AGENTS_DIR
    q_rel = f"{skills_rel}/{_QUARANTINE_DIR}"
    try:
        safe_fs.mkdirs_beneath(root, q_rel)
    except safe_fs.SymlinkRefused:
        safe_fs.unlink_beneath(root, q_rel)
        logger.warning("skills reconcile: a link planted at %s was replaced by a directory", q_rel)
        safe_fs.mkdirs_beneath(root, q_rel)
    dest = name
    n = 1
    while _present(f"{q_rel}/{dest}"):
        dest = f"{name}-{n}"
        n += 1
    safe_fs.rename_beneath(root, f"{skills_rel}/{name}", f"{q_rel}/{dest}")
    logger.warning(
        "skills reconcile: quarantined unmanaged entry %s -> %s "
        "(platform-managed dir)",
        f"{skills_rel}/{name}", f"{q_rel}/{dest}",
    )
    # Bounded: keep the newest N quarantined entries.
    step = _entries(q_rel)
    aged: list[tuple[float, str]] = []
    for entry in step.dirs + step.files + step.symlinks + step.other:
        try:
            aged.append((safe_fs.lstat_beneath(root, f"{q_rel}/{entry}").st_mtime, entry))
        except OSError:
            continue
    aged.sort(reverse=True)
    for _mtime, entry in aged[_QUARANTINE_KEEP:]:
        _remove_entry(f"{q_rel}/{entry}")


def materialize_skills_for_sandbox(agent_name: str, config_dir: Path, *,
                                   username: str = "") -> None:
    """Reconcile ``<config_dir>/skills/`` to the agent's enabled on-demand set.

    ``username`` is the person whose own config dir this is: a skill whose
    ``audience`` their role on the agent fails is left out (one person, one
    role, so every session sharing the dir computes the same set); the
    agent-level dir keeps the role-free set.

    Fail-soft at every level: any error logs and leaves the session start
    unaffected (hooks stay fail-hard; skills never block a session).
    """
    try:
        _materialize_locked(agent_name, Path(config_dir), username=username)
    except Exception:
        logger.exception(
            "skills materialization failed for agent=%s dir=%s — session "
            "proceeds without on-demand skills", agent_name, config_dir,
        )


def _lock_path(config_dir: Path) -> Path:
    """The lock file for one config dir, in a proxy-owned directory no
    sandbox mounts (keyed by the dir's real path)."""
    import config
    locks = Path(config.SESSIONS_DIR) / _LOCKS_DIRNAME
    os.makedirs(locks, mode=0o700, exist_ok=True)
    digest = hashlib.sha256(os.path.realpath(config_dir).encode()).hexdigest()[:32]
    return locks / f"{digest}.lock"


def _materialize_locked(agent_name: str, config_dir: Path, *, username: str = "") -> None:
    import config
    from services.mcp import mcp_registry

    wanted = mcp_registry.get_on_demand_skills_for_materialization(
        agent_name, username=username or None)
    skills_dir = config_dir / "skills"
    if not wanted and not skills_dir.is_dir():
        return  # nothing to add, nothing to reconcile — don't create churn

    # Every write below is beneath AGENTS_DIR: a config dir lives in the
    # agent tree (users/<u>/.claude, workspace/.claude, an external's home).
    cfg_rel = safe_fs.rel_under(config_dir, config.AGENTS_DIR)
    if not cfg_rel:
        raise ValueError(f"config dir {config_dir} is not an agent's")
    skills_rel = f"{cfg_rel}/skills"
    safe_fs.mkdirs_beneath(config.AGENTS_DIR, skills_rel)
    lock_fd = os.open(_lock_path(config_dir), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        _reconcile(wanted, skills_rel)
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _reconcile(wanted: list[tuple[str, Path, str, str, str]], skills_rel: str) -> None:
    """Every step beneath ``AGENTS_DIR`` by rel, no link followed: the
    skills dir is writable from inside the sandbox, so a link swapped in at
    any name after a check must refuse the step, never redirect it."""
    import config

    root = config.AGENTS_DIR
    # Clear staging debris from crashed prior runs (we hold the lock; live
    # CLIs never read dot-prefixed dirs).
    step = _entries(skills_rel)
    for name in step.dirs + step.files + step.symlinks + step.other:
        if name.startswith(".staging-"):
            _remove_entry(f"{skills_rel}/{name}")

    materialized: set[str] = set()
    for skill_id, source, pkg, version, description in wanted:
        if not source.is_file():
            logger.warning("skill %s: source %s missing, skipped",
                           skill_id, source)
            continue
        staging_rel = f"{skills_rel}/.staging-{skill_id}-{os.getpid()}"
        target_rel = f"{skills_rel}/{skill_id}"
        try:
            _stage_skill(source, skill_id, description, staging_rel)
            expected = _skill_tree_digest(staging_rel)
            safe_fs.atomic_write_beneath(
                root, f"{staging_rel}/{_MARKER_NAME}",
                (json.dumps({"package": pkg, "version": version, "digest": expected},
                            indent=2) + "\n").encode("utf-8"),
                fsync=False,
            )
            same = False
            if _is_dir(target_rel):
                try:
                    same = _skill_tree_digest(target_rel) == expected
                except safe_fs.SafeFsError:
                    same = False
            if same:
                safe_fs.rmtree_beneath(root, staging_rel)
            else:
                old_rel = f"{skills_rel}/.staging-old-{skill_id}-{os.getpid()}"
                if _present(target_rel):
                    safe_fs.rename_beneath(root, target_rel, old_rel)
                safe_fs.rename_beneath(root, staging_rel, target_rel)
                _remove_entry(old_rel)
                logger.info("skill %s materialized (pkg=%s v=%s)",
                            skill_id, pkg, version)
            materialized.add(skill_id)
        except Exception:
            logger.exception("skill %s: materialization failed, skipped",
                             skill_id)
            try:
                _remove_entry(staging_rel)
            except OSError:
                logger.warning("skill %s: staging debris left at %s", skill_id, staging_rel)

    # Reconcile removals: quarantine non-dot entries not in the enabled set
    # (disabled or uninstalled skills, on_demand to always restamps,
    # agent-written strays). Dot-prefixed entries (Codex's .system builtins,
    # the lock, .quarantine itself) are never touched.
    step = _entries(skills_rel)
    for name in step.dirs + step.files + step.symlinks + step.other:
        if name.startswith(".") or name in materialized:
            continue
        try:
            _quarantine(skills_rel, name)
        except Exception:
            logger.exception("skills reconcile: failed to quarantine %s", name)
