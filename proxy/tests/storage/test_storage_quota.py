"""Tests for services/infra/storage_quota.py — the quota scope model, the project-ID
allocator, usage measurement, and the kernel-tier boot preflight.

The kernel tier's actual XFS enforcement can't run here (needs root + an XFS
mount with project quotas enabled); those paths are exercised live on a real
XFS-pquota volume. These tests cover everything privilege-free:
identity, limits, allocation, measurement, and preflight gating.
"""

import pytest

import config
from services.infra import storage_quota as sq
from storage import database
from storage.agents import agent_store
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
    # The three workspace roots plus the shared apps' release copies and
    # databases (APPS.md: both live outside the workspace).
    assert [d.relative_to(tmp_path / "acme").as_posix() for d in dirs] == [
        "workspace", "knowledge", "config", "app-releases/shared", "app-data/shared"]
    assert sq.user_scope_dir("acme", "bob") == tmp_path / "acme" / "users" / "bob"
    # One user's bucket: their tree, their apps' releases and databases,
    # their chat snapshots.
    assert [d.relative_to(tmp_path / "acme").as_posix() for d in sq.user_scope_dirs("acme", "bob")] == [
        "users/bob", "app-releases/users/bob", "app-data/users/bob", "shares/users/bob"]


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
    assert len(shared.dirs) == 5
    assert shared.owner_sub is None

    user = by_key["user:acme:mgr"]
    assert user.scope_type == "user"
    assert user.owner_sub == "user-manager"
    assert user.dirs == tuple(sq.user_scope_dirs("acme", "mgr"))
    assert user.dirs[0] == tmp_path / "acme" / "users" / "mgr"


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


# --- the helper's paths: never a link inside a scope, never odd text --------

def _load_helper():
    """The root helper, loaded in this process from its file (it imports
    nothing from the app), so the mount table, the attribute read and
    ``xfs_quota`` can be stand-ins: no real command runs."""
    import importlib.machinery
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[2] / "scripts" / "oto-quota"
    loader = importlib.machinery.SourceFileLoader("oto_quota_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def helper(monkeypatch):
    """The helper with stand-ins: every path sits on an XFS mount with
    project quotas, nothing is stamped yet, and ``run`` records each
    ``xfs_quota`` command instead of starting it."""
    h = _load_helper()
    calls: list[list[str]] = []
    monkeypatch.setattr(h, "mount_info", lambda p: ("/", "xfs", "rw,prjquota"))
    monkeypatch.setattr(h, "need_tool", lambda name: None)
    monkeypatch.setattr(h, "stamped", lambda fd, pid: False)

    def _run(argv, check=True):
        import subprocess
        assert argv[0] == "xfs_quota", argv
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
    monkeypatch.setattr(h, "run", _run)
    h.calls = calls
    return h


def _through(helper, monkeypatch):
    """Route the proxy's helper calls into the loaded helper, as ``sudo -n``
    would, and answer like ``_run_helper``: a refusal is a RuntimeError."""
    import subprocess

    def _run_helper(*args, check=True, timeout=120):
        try:
            helper._COMMANDS[args[0]]([str(a) for a in args[1:]])
        except SystemExit as e:
            if check and e.code:
                raise RuntimeError(f"oto-quota {args[0]} failed (rc={e.code})") from None
            return subprocess.CompletedProcess(args, e.code or 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="ok", stderr="")
    monkeypatch.setattr(sq, "_run_helper", _run_helper)
    monkeypatch.setattr(sq, "_hard_enabled", True)
    monkeypatch.setattr(sq, "_assigned_this_process", set())
    monkeypatch.setattr(sq, "get_or_alloc_project", lambda *a, **k: sq._PROJECT_ID_BASE + 7)


def _stamped_paths(helper) -> list[str]:
    return [c[3].split()[3] for c in helper.calls if c[3].startswith("project -s -p ")]


def test_a_data_folder_reached_through_a_link_keeps_its_quotas(helper, monkeypatch, tmp_path):
    """The install's own data folder is a link (moved to another disk): the
    proxy resolves it once and the helper stamps every folder of the scope by
    its real path. Quotas keep working there."""
    real = tmp_path / "real-data"
    real.mkdir()
    link = tmp_path / "data-link"
    link.symlink_to(real)
    monkeypatch.setattr(config, "AGENTS_DIR", link)
    _through(helper, monkeypatch)
    assert sq.ensure_scope("acme", "user", "bob") == sq._PROJECT_ID_BASE + 7
    stamped = _stamped_paths(helper)
    assert stamped and all(p.startswith(str(real) + "/acme/") for p in stamped)
    assert str(real / "acme" / "users" / "bob") in stamped
    assert "user:acme:bob" in sq._assigned_this_process


def test_a_link_inside_a_scope_is_never_followed(helper, monkeypatch, tmp_path):
    """A scope folder that is a link: the proxy makes nothing through it and
    stamps nothing where it points."""
    agents = tmp_path / "agents"
    (agents / "acme" / "users").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (agents / "acme" / "users" / "bob").symlink_to(elsewhere)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    _through(helper, monkeypatch)
    assert sq.ensure_scope("acme", "user", "bob") is None
    assert not any(str(elsewhere) in p for p in _stamped_paths(helper))
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("make", [
    lambda base: str(base / "with space"),
    lambda base: str(base / "quote'd"),
    lambda base: str(base) + "/" + "." + "./" + base.name,
    lambda base: "relative/path",
    lambda base: str(base / "link-part" / "inside"),
], ids=["a-space", "a-quote", "a-parent-step", "relative", "a-link-on-the-way"])
def test_the_helper_refuses_a_path_that_is_not_plain(helper, tmp_path, make):
    base = tmp_path / "plain"
    for name in ("with space", "quote'd", "real-target/inside"):
        (base / name).mkdir(parents=True)
    (base / "link-part").symlink_to(base / "real-target")
    with pytest.raises(SystemExit) as refused:
        helper.cmd_assign([make(base), str(helper.PROJECT_ID_BASE + 1)])
    assert refused.value.code == 1
    assert helper.calls == []


def test_the_helper_stamps_a_plain_folder_once(helper, monkeypatch, tmp_path):
    folder = tmp_path / "plain" / "acme" / "workspace"
    folder.mkdir(parents=True)
    pid = helper.PROJECT_ID_BASE + 3
    helper.cmd_assign([str(folder), str(pid)])
    assert helper.calls == [["xfs_quota", "-x", "-c", f"project -s -p {folder} {pid}", "/"]]
    monkeypatch.setattr(helper, "stamped", lambda fd, p: p == pid)
    helper.cmd_assign([str(folder), str(pid)])
    assert len(helper.calls) == 1


def test_the_helper_reads_a_folders_project_without_a_walk(helper, tmp_path):
    """The attribute read answers for the folder the helper opened: on a
    folder never stamped (this box's disk) it is not stamped."""
    h = _load_helper()
    import os
    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert h.stamped(fd, h.PROJECT_ID_BASE + 1) is False
    finally:
        os.close(fd)


def test_the_proxy_and_the_helper_share_one_rule_for_a_plain_part(helper):
    assert sq._PLAIN_PART.pattern == helper.PLAIN_PART.pattern


@pytest.mark.parametrize("as_root", [True, False], ids=["root", "through-sudo"])
def test_preflight_stays_soft_on_a_data_folder_the_helper_would_refuse(monkeypatch, tmp_path, as_root):
    agents = tmp_path / "data dir"
    agents.mkdir()
    monkeypatch.setattr(config, "STORAGE_QUOTAS_FORCE_SOFT", False)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(sq, "_is_root", lambda: as_root)
    monkeypatch.setattr(sq, "_mount_info", lambda p: ("xfs", "rw,prjquota"))
    helper_file = tmp_path / "oto-quota"
    helper_file.write_text("#!/bin/sh\n")
    monkeypatch.setattr(config, "OTODOCK_QUOTA_HELPER", str(helper_file))
    monkeypatch.setattr(sq, "_run_helper", lambda *a, **k: None)
    sq.quotas_preflight()
    assert sq.hard_enabled() is False


def test_a_scope_the_helper_would_refuse_is_logged_once(helper, monkeypatch, tmp_path, caplog):
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    monkeypatch.setattr(config, "get_agent_dir", lambda slug: agents / slug)
    _through(helper, monkeypatch)
    with caplog.at_level("WARNING", logger=sq.logger.name):
        assert sq.ensure_scope("odd name", "shared") is None
        assert sq.ensure_scope("odd name", "shared") is None
    assert helper.calls == []
    assert len([r for r in caplog.records if "odd name" in r.getMessage()]) == 1


def test_dir_usage_of_an_agents_folder_follows_no_link_below_it(tmp_path, monkeypatch):
    """A scope folder replaced by a link counts nothing of its target: the
    walk runs beneath the agent's real folder with no link followed."""
    agents = tmp_path / "agents"
    (agents / "pa" / "users").mkdir(parents=True)
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    stand_in = tmp_path / "stand-in"
    stand_in.mkdir()
    (stand_in / "big").write_bytes(b"z" * 5000)
    (agents / "pa" / "users" / "alice").symlink_to(stand_in)
    assert sq.dir_usage(agents / "pa" / "users" / "alice") == (0, 0)


def test_dir_usage_counts_an_agent_behind_the_operators_link(tmp_path, monkeypatch):
    """An agent folder an admin moved behind a link (the operator's own
    layout) is counted in its real folder, as any other agent's."""
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(config, "AGENTS_DIR", agents)
    real = tmp_path / "disk2" / "pa"
    (real / "users" / "alice").mkdir(parents=True)
    (real / "users" / "alice" / "a").write_bytes(b"x" * 100)
    (real / "workspace").mkdir()
    (real / "workspace" / "b").write_bytes(b"y" * 50)
    (agents / "pa").symlink_to(real)
    assert sq.dir_usage(agents / "pa") == (150, 2)
    assert sq.dir_usage(agents / "pa" / "users" / "alice") == (100, 1)
    assert sq.dir_usage(agents / "pa")[0] < 5000


@pytest.mark.parametrize("cgroup, refused", [
    ("0::/system.slice/otodock-compute.service\n", True),
    ("12:pids:/system.slice/otodock-compute.service\n0::/system.slice/otodock-compute.service\n", True),
    ("0::/system.slice/otodock-proxy.service\n", False),
    ("0::/\n", False),
    ("0::/user.slice/user-1000.slice/session-3.scope\n", False),
])
def test_the_helper_refuses_a_caller_from_the_compute_unit(tmp_path, monkeypatch, capsys, cgroup, refused):
    """sudo's child stays in its caller's cgroup: a call from the compute
    unit is refused before any subcommand runs; the proxy's unit, a
    container and an operator's shell are not."""
    h = _load_helper()
    (tmp_path / "cgroup").write_text(cgroup)
    monkeypatch.setattr(h, "CGROUP_FILE", str(tmp_path / "cgroup"))
    ran = []
    monkeypatch.setattr(h, "_COMMANDS", {"check": ran.append})
    monkeypatch.setattr(h.sys, "argv", ["oto-quota", "check", "/"])
    if refused:
        with pytest.raises(SystemExit) as e:
            h.main()
        assert e.value.code == 1 and ran == []
        assert "compute" in capsys.readouterr().err
    else:
        h.main()
        assert ran == [["/"]]


def test_the_helper_refuses_when_it_cannot_read_its_cgroup(tmp_path, monkeypatch):
    h = _load_helper()
    monkeypatch.setattr(h, "CGROUP_FILE", str(tmp_path / "missing"))
    monkeypatch.setattr(h, "_COMMANDS", {"check": lambda args: None})
    monkeypatch.setattr(h.sys, "argv", ["oto-quota", "check", "/"])
    with pytest.raises(SystemExit):
        h.main()
