"""Tests for services/infra/storage_quota.py — the quota scope model, the project-ID
allocator, usage measurement, and the kernel-tier boot preflight.

The kernel tier's actual XFS enforcement can't run here (needs root + an XFS
mount with project quotas enabled); those paths are exercised live on a real
XFS-pquota volume. These tests cover everything privilege-free:
identity, limits, allocation, measurement, and preflight gating.
"""

import config
import pytest
from services.infra import storage_quota as sq
from storage.agents import agent_store
from storage import database
from storage.pg import get_conn


# --- helpers ---------------------------------------------------------------

def _mk_agent(slug="acme", shared_only=False):
    if shared_only:
        agent_store.create_agent(
            slug, slug.title(), default_scope="agent", collaborative=False,
        )
    else:
        agent_store.create_agent(slug, slug.title())


def _set_username(sub, username):
    with get_conn() as c:
        c.execute("UPDATE users SET username=%s WHERE sub=%s", (username, sub))
        c.commit()


def _attach(sub, slug, role):
    database.set_user_agents(sub, [slug], "user-admin", {slug: role})


@pytest.fixture(autouse=True)
def _reset_hard_enabled():
    """quotas_preflight() sets the module-global _hard_enabled directly, so reset
    it around every test to avoid leaking enforcement state between tests."""
    sq._hard_enabled = False
    yield
    sq._hard_enabled = False


# --- scope identity + layout ----------------------------------------------

def test_scope_keys():
    assert sq.shared_scope_key("acme") == "acme:shared"
    assert sq.user_scope_key("acme", "bob") == "user:acme:bob"


def test_scope_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    dirs = sq.shared_scope_dirs("acme")
    assert [d.name for d in dirs] == ["workspace", "knowledge", "config"]
    assert all(d.parent == tmp_path / "acme" for d in dirs)
    assert sq.user_scope_dir("acme", "bob") == tmp_path / "acme" / "users" / "bob"


# --- limits (settings are the single source of truth) ----------------------

def test_limits_defaults():
    sb, si = sq.limits_for("shared")
    assert sb == config.QUOTA_SHARED_FOLDER_MB_DEFAULT * 1024 * 1024
    assert si == 0  # inode caps default off
    ub, ui = sq.limits_for("user")
    assert ub == config.QUOTA_USER_FOLDER_MB_DEFAULT * 1024 * 1024
    assert ui == 0


def test_limits_custom_and_unlimited():
    database.set_platform_setting("quota_shared_folder_mb", "100")
    database.set_platform_setting("quota_user_folder_mb", "0")  # 0 = unlimited
    database.set_platform_setting("quota_shared_folder_inodes", "5000")
    assert sq.limits_for("shared") == (100 * 1024 * 1024, 5000)
    assert sq.limits_for("user") == (0, 0)


def test_limits_garbage_falls_back_to_default():
    database.set_platform_setting("quota_shared_folder_mb", "not-a-number")
    assert sq.limits_for("shared")[0] == config.QUOTA_SHARED_FOLDER_MB_DEFAULT * 1024 * 1024


# --- project-ID allocator --------------------------------------------------

def test_allocator_monotonic_and_idempotent():
    a = sq.get_or_alloc_project("k1", "ag", "shared", None)
    b = sq.get_or_alloc_project("k2", "ag", "user", "bob")
    assert a == sq._PROJECT_ID_BASE
    assert b == a + 1
    # idempotent — same scope_key returns the same id
    assert sq.get_or_alloc_project("k1", "ag", "shared", None) == a
    assert sq.get_project_id("k1") == a
    assert sq.get_project_id("missing") is None


def test_allocator_never_reuses_freed_id():
    a = sq.get_or_alloc_project("k1", "ag", "shared", None)
    b = sq.get_or_alloc_project("k2", "ag", "shared", None)
    # reclaim keeps the row as a tombstone (flag off here → just drops the memo),
    # so MAX() still sees it and the next id keeps climbing.
    sq.reclaim_project("k2")
    c = sq.get_or_alloc_project("k3", "ag", "shared", None)
    assert (a, b, c) == (sq._PROJECT_ID_BASE, sq._PROJECT_ID_BASE + 1, sq._PROJECT_ID_BASE + 2)


def test_list_projects():
    sq.get_or_alloc_project("k1", "ag", "shared", None)
    sq.get_or_alloc_project("k2", "ag", "user", "bob")
    rows = sq.list_projects()
    assert {r["scope_key"] for r in rows} == {"k1", "k2"}
    assert all(r["project_id"] >= sq._PROJECT_ID_BASE for r in rows)


# --- usage measurement -----------------------------------------------------

def test_dir_usage_sums_bytes_and_counts_files(tmp_path):
    (tmp_path / "a").write_bytes(b"x" * 100)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 50)
    assert sq.dir_usage(tmp_path) == (150, 2)


def test_dir_usage_missing_dir():
    assert sq.dir_usage(config.AGENTS_DIR / "does-not-exist") == (0, 0)


def test_dir_usage_does_not_follow_symlinks(tmp_path):
    big = tmp_path / "big"
    big.write_bytes(b"z" * 5000)
    d = tmp_path / "d"
    d.mkdir()
    (d / "link").symlink_to(big)
    used, count = sq.dir_usage(d)
    assert count == 1
    assert used < 5000  # lstat of the link, not the 5000-byte target


# --- scope enumeration -----------------------------------------------------

def test_iter_scopes(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    _mk_agent("acme")
    _mk_agent("sharedbot", shared_only=True)
    _set_username("user-manager", "mgr")
    _set_username("user-viewer", "vwr")
    _attach("user-manager", "acme", "manager")
    _attach("user-viewer", "acme", "viewer")

    scopes = sq.iter_scopes()
    by_key = {s.scope_key: s for s in scopes}

    assert "acme:shared" in by_key
    assert "user:acme:mgr" in by_key
    assert "user:acme:vwr" in by_key
    # Shared-only agents are metered (shared scope, all modes the same) but have
    # no per-user dirs → a shared scope, never a user scope.
    assert "sharedbot:shared" in by_key
    assert not any(
        s.scope_type == "user" and s.agent_slug == "sharedbot" for s in scopes
    )

    shared = by_key["acme:shared"]
    assert shared.scope_type == "shared"
    assert len(shared.dirs) == 3
    assert shared.owner_sub is None

    user = by_key["user:acme:mgr"]
    assert user.scope_type == "user"
    assert user.owner_sub == "user-manager"
    assert user.dirs == (tmp_path / "acme" / "users" / "mgr",)


def test_iter_scopes_skips_user_without_username(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    _mk_agent("acme")
    _attach("user-viewer", "acme", "viewer")  # seeded user has no username slug
    scopes = sq.iter_scopes()
    assert not any(s.scope_type == "user" for s in scopes)
    assert any(s.scope_key == "acme:shared" for s in scopes)


# --- hard-tier guards (no-op when enforcement is not active) ---------------

def test_ensure_scope_noop_when_soft(monkeypatch):
    monkeypatch.setattr(sq, "_hard_enabled", False)
    assert sq.ensure_scope("acme", "shared") is None
    assert sq.ensure_scope("acme", "user", "bob") is None


def test_report_usage_none_when_soft(monkeypatch):
    monkeypatch.setattr(sq, "_hard_enabled", False)
    assert sq.report_usage(sq._PROJECT_ID_BASE) is None


def test_reapply_all_limits_noop_when_soft(monkeypatch):
    monkeypatch.setattr(sq, "_hard_enabled", False)
    sq.reapply_all_limits()  # must not raise / shell out


# --- boot preflight: detect-and-degrade (never raises) ---------------------

def test_preflight_soft_when_force_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STORAGE_QUOTAS_FORCE_SOFT", True)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(sq, "_mount_info", lambda p: ("xfs", "rw,prjquota"))
    sq.quotas_preflight()
    assert sq.hard_enabled() is False


def test_preflight_enables_hard_on_xfs_with_project_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STORAGE_QUOTAS_FORCE_SOFT", False)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(sq, "_is_root", lambda: True)
    monkeypatch.setattr(sq, "_mount_info", lambda p: ("xfs", "rw,relatime,prjquota"))
    sq.quotas_preflight()
    assert sq.hard_enabled() is True


def test_preflight_degrades_to_soft_on_non_xfs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STORAGE_QUOTAS_FORCE_SOFT", False)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(sq, "_is_root", lambda: True)
    monkeypatch.setattr(sq, "_mount_info", lambda p: ("ext4", "rw,relatime"))
    sq.quotas_preflight()  # never raises
    assert sq.hard_enabled() is False


def test_preflight_degrades_on_xfs_without_project_quota(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STORAGE_QUOTAS_FORCE_SOFT", False)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(sq, "_is_root", lambda: True)
    monkeypatch.setattr(sq, "_mount_info", lambda p: ("xfs", "rw,relatime"))
    sq.quotas_preflight()
    assert sq.hard_enabled() is False


def test_preflight_degrades_when_helper_unreachable_as_nonroot(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STORAGE_QUOTAS_FORCE_SOFT", False)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(sq, "_is_root", lambda: False)
    monkeypatch.setattr(sq, "_mount_info", lambda p: ("xfs", "rw,relatime,prjquota"))
    monkeypatch.setattr(config, "OTODOCK_QUOTA_HELPER", str(tmp_path / "no-such-helper"))
    sq.quotas_preflight()  # never raises — missing helper → soft
    assert sq.hard_enabled() is False


def test_preflight_degrades_when_helper_present_but_no_sudo(monkeypatch, tmp_path):
    """The non-root T2 path: capable XFS fs, helper EXISTS, but invoking it via
    `sudo -n` raises FileNotFoundError (no sudo in the container). The broad
    `except Exception` in quotas_preflight must swallow it and degrade to soft —
    NOT brick boot. Guards against a regression that narrows that except to e.g.
    CalledProcessError (which would let the FileNotFoundError propagate)."""
    helper = tmp_path / "oto-quota"
    helper.write_text("#!/bin/sh\n")  # exists → passes the existence gate
    monkeypatch.setattr(config, "STORAGE_QUOTAS_FORCE_SOFT", False)
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(sq, "_is_root", lambda: False)
    monkeypatch.setattr(sq, "_mount_info", lambda p: ("xfs", "rw,relatime,prjquota"))
    monkeypatch.setattr(config, "OTODOCK_QUOTA_HELPER", str(helper))

    def _no_sudo(*a, **k):  # what subprocess.run(["sudo", ...]) does sans sudo
        raise FileNotFoundError("[Errno 2] No such file or directory: 'sudo'")
    monkeypatch.setattr(sq.subprocess, "run", _no_sudo)

    sq.quotas_preflight()  # must NOT raise
    assert sq.hard_enabled() is False


def test_mount_info_resolves_a_real_path():
    # Smoke: the agents dir resolves to *some* mount with a fstype + options.
    info = sq._mount_info(config.AGENTS_DIR)
    assert info is None or (isinstance(info[0], str) and isinstance(info[1], str))
