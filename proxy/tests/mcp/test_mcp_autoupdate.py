"""Automatic MCP updates — orchestrator + updater helpers.

DB-free: every collaborator (detection, single-MCP update, in-use probe, the log
store, the notifier) is monkeypatched, so these exercise the decision logic only.
"""

import datetime as dt

import pytest

from services.mcp import mcp_autoupdate as au
from storage.mcp import mcp_autoupdate_store as log_store


# --- stand-in manifests --------------------------------------------------

class _Srv:
    def __init__(self, runtime):
        self.runtime = runtime
        self.image = ""
        self.source = ""


class _Manifest:
    def __init__(self, name, runtime="docker", category="community"):
        self.name = name
        self.category = category
        self.server = _Srv(runtime)


# --- fixtures ------------------------------------------------------------

@pytest.fixture
def log_rows(monkeypatch):
    rows = []

    def _rec(run_id, mcp_name, *, runtime="", old_version="", new_version="",
             status, error="", trigger="auto"):
        rows.append({"mcp": mcp_name, "runtime": runtime, "status": status,
                     "old": old_version, "new": new_version, "error": error})

    monkeypatch.setattr(au.log_store, "record_result", _rec)
    return rows


@pytest.fixture
def notify_calls(monkeypatch):
    calls = []

    async def _fire(**kw):
        calls.append(kw)
        return []

    import services.notifications.notification_manager as nm
    monkeypatch.setattr(nm, "fire_notification", _fire)
    return calls


def _patch_updater(monkeypatch, *, targets, updates, update_result=None,
                   update_exc=None, in_use=False):
    monkeypatch.setattr(au.mcp_updater, "community_targets", lambda: targets)

    async def _detect():
        return {"updates": updates}
    monkeypatch.setattr(au.mcp_updater, "detect_available_updates", _detect)

    called = {"update": []}

    async def _update_one(name):
        called["update"].append(name)
        if update_exc:
            raise update_exc
        return update_result or {"status": "updated", "old_version": "1.0",
                                 "version": "2.0"}
    monkeypatch.setattr(au.mcp_updater, "update_one", _update_one)

    async def _in_use(name):
        return in_use(name) if callable(in_use) else in_use
    monkeypatch.setattr(au.mcp_updater, "mcp_in_use", _in_use)
    return called


# --- orchestrator --------------------------------------------------------

@pytest.mark.asyncio
async def test_stdio_updates_immediately_no_defer(monkeypatch, log_rows, notify_calls):
    """npm/pypi MCPs update without any in-use check."""
    m = _Manifest("foo", runtime="python")
    called = _patch_updater(
        monkeypatch, targets=[m],
        updates={"foo": {"current": "1.0", "latest": "2.0"}},
        in_use=lambda n: pytest.fail("stdio MCP must not be in-use-checked"),
    )
    summary = await au.run_auto_update(trigger="auto")
    assert called["update"] == ["foo"]
    assert summary["counts"][log_store.STATUS_UPDATED] == 1
    assert [r["status"] for r in log_rows] == ["updated"]
    assert notify_calls == []


@pytest.mark.asyncio
async def test_docker_free_updates_now(monkeypatch, log_rows, notify_calls):
    m = _Manifest("bar", runtime="docker")
    called = _patch_updater(
        monkeypatch, targets=[m],
        updates={"bar": {"current": "1.0", "latest": "2.0"}},
        in_use=False,
    )
    summary = await au.run_auto_update()
    assert called["update"] == ["bar"]
    assert summary["counts"][log_store.STATUS_UPDATED] == 1
    assert notify_calls == []


@pytest.mark.asyncio
async def test_docker_in_use_deferred_then_skipped(monkeypatch, log_rows, notify_calls):
    """An always-in-use docker MCP is never updated; after the timeout it is
    logged skipped_in_use (NOT a failure → no notification)."""
    monkeypatch.setattr(au, "DEFER_TIMEOUT_S", 0)
    monkeypatch.setattr(au, "DEFER_RECHECK_S", 0)
    m = _Manifest("busy", runtime="docker")
    called = _patch_updater(
        monkeypatch, targets=[m],
        updates={"busy": {"current": "1.0", "latest": "2.0"}},
        in_use=True,
    )
    summary = await au.run_auto_update()
    assert called["update"] == []  # never updated while in use
    assert summary["counts"][log_store.STATUS_SKIPPED_IN_USE] == 1
    assert summary["counts"][log_store.STATUS_FAILED] == 0
    assert [r["status"] for r in log_rows] == ["skipped_in_use"]
    assert notify_calls == []


@pytest.mark.asyncio
async def test_failure_logs_and_notifies_once(monkeypatch, log_rows, notify_calls):
    m = _Manifest("baz", runtime="docker")
    _patch_updater(
        monkeypatch, targets=[m],
        updates={"baz": {"current": "1.0", "latest": "2.0"}},
        in_use=False, update_exc=RuntimeError("boom"),
    )
    summary = await au.run_auto_update()
    assert summary["counts"][log_store.STATUS_FAILED] == 1
    assert [r["status"] for r in log_rows] == ["failed"]
    assert len(notify_calls) == 1
    assert notify_calls[0]["scope"] == "admin"
    assert notify_calls[0]["severity"] == "warning"


@pytest.mark.asyncio
async def test_update_outside_community_targets_ignored(monkeypatch, log_rows, notify_calls):
    """A detected update for an MCP not in community_targets is skipped."""
    m = _Manifest("bar", runtime="docker")
    called = _patch_updater(
        monkeypatch, targets=[m],
        updates={"some-core-mcp": {"current": "1.0", "latest": "2.0"}},
        in_use=False,
    )
    summary = await au.run_auto_update()
    assert called["update"] == []
    assert all(c == 0 for c in summary["counts"].values())
    assert log_rows == []
    assert notify_calls == []


@pytest.mark.asyncio
async def test_mixed_run_one_failure_one_success(monkeypatch, log_rows, notify_calls):
    stdio = _Manifest("foo", runtime="python")
    docker = _Manifest("bar", runtime="docker")

    async def _update_one(name):
        if name == "bar":
            raise RuntimeError("kaboom")
        return {"status": "updated", "old_version": "1.0", "version": "2.0"}

    monkeypatch.setattr(au.mcp_updater, "community_targets", lambda: [stdio, docker])

    async def _detect():
        return {"updates": {"foo": {"current": "1.0", "latest": "2.0"},
                            "bar": {"current": "1.0", "latest": "2.0"}}}
    monkeypatch.setattr(au.mcp_updater, "detect_available_updates", _detect)
    monkeypatch.setattr(au.mcp_updater, "update_one", _update_one)

    async def _in_use(name):
        return False
    monkeypatch.setattr(au.mcp_updater, "mcp_in_use", _in_use)

    summary = await au.run_auto_update()
    assert summary["counts"][log_store.STATUS_UPDATED] == 1
    assert summary["counts"][log_store.STATUS_FAILED] == 1
    # exactly one failure notification, listing the failed MCP
    assert len(notify_calls) == 1


@pytest.mark.asyncio
async def test_held_mcp_skipped(monkeypatch, log_rows, notify_calls, tmp_path):
    """A .hold marker in the MCP dir excludes it from the weekly converge —
    recorded ``held`` (not a failure → no notification)."""
    from services.mcp import mcp_updater

    m = _Manifest("ahead", runtime="docker")
    m.mcp_dir = tmp_path
    (tmp_path / mcp_updater.HOLD_MARKER).touch()
    called = _patch_updater(
        monkeypatch, targets=[m],
        updates={"ahead": {"current": "0.0.70", "latest": "0.0.69",
                           "downgrade": True}},
        in_use=lambda n: pytest.fail("held MCP must not be in-use-checked"),
    )
    summary = await au.run_auto_update()
    assert called["update"] == []
    assert summary["counts"][log_store.STATUS_HELD] == 1
    assert [r["status"] for r in log_rows] == ["held"]
    assert notify_calls == []


@pytest.mark.asyncio
async def test_unheld_downgrade_applies_but_warns(
    monkeypatch, log_rows, notify_calls, caplog,
):
    """Without a hold the converge still applies (the catalog is the version
    of record — rollbacks must land) but the run warns loudly."""
    import logging

    m = _Manifest("ahead", runtime="docker")
    called = _patch_updater(
        monkeypatch, targets=[m],
        updates={"ahead": {"current": "0.0.70", "latest": "0.0.69",
                           "downgrade": True}},
        in_use=False,
    )
    with caplog.at_level(logging.WARNING, logger="claude-proxy.mcp-autoupdate"):
        summary = await au.run_auto_update()
    assert called["update"] == ["ahead"]
    assert summary["counts"][log_store.STATUS_UPDATED] == 1
    assert any("DOWNGRAD" in r.message for r in caplog.records)


# --- community_targets (tier filtering) ----------------------------------

def _patch_targets(monkeypatch, manifests, mode):
    monkeypatch.setattr(
        au.mcp_updater.mcp_registry, "get_all_manifests",
        lambda: {m.name: m for m in manifests},
    )
    from core.config import deployment
    monkeypatch.setattr(deployment, "current_mode", lambda: mode)


def test_community_targets_excludes_docker_on_t3(monkeypatch):
    from core.config import deployment
    _patch_targets(monkeypatch, [
        _Manifest("d", runtime="docker"),
        _Manifest("p", runtime="python"),
        _Manifest("core-x", runtime="docker", category="core"),
    ], deployment.EXTERNAL_POOL)
    names = {m.name for m in au.mcp_updater.community_targets()}
    assert names == {"p"}  # docker dropped on T3, core never included


def test_community_targets_includes_docker_self_host(monkeypatch):
    from core.config import deployment
    _patch_targets(monkeypatch, [
        _Manifest("d", runtime="docker"),
        _Manifest("p", runtime="python"),
        _Manifest("custom-x", runtime="python", category="custom"),
    ], deployment.MANAGED_LOCAL)
    names = {m.name for m in au.mcp_updater.community_targets()}
    assert names == {"d", "p"}  # both community runtimes; custom excluded


# --- scheduling gate -----------------------------------------------------

def test_is_due_no_prior_run(monkeypatch):
    monkeypatch.setattr(au.task_store, "get_platform_setting", lambda k: "")
    due, overdue = au._is_due(au._now_utc())
    assert due and overdue


def test_is_due_recent_run_not_due(monkeypatch):
    recent = (au._now_utc() - dt.timedelta(days=1)).isoformat()
    monkeypatch.setattr(au.task_store, "get_platform_setting", lambda k: recent)
    due, overdue = au._is_due(au._now_utc())
    assert not due and not overdue


def test_is_due_overdue_catch_up(monkeypatch):
    old = (au._now_utc() - dt.timedelta(days=9)).isoformat()
    monkeypatch.setattr(au.task_store, "get_platform_setting", lambda k: old)
    due, overdue = au._is_due(au._now_utc())
    assert due and overdue


def test_in_window_sunday_vs_monday(monkeypatch):
    import config
    monkeypatch.setattr(config, "get_platform_timezone", lambda: "UTC")
    sunday_3am = dt.datetime(2024, 6, 30, 3, 30, tzinfo=dt.timezone.utc)   # Sun
    sunday_6am = dt.datetime(2024, 6, 30, 6, 0, tzinfo=dt.timezone.utc)    # Sun, late
    monday_3am = dt.datetime(2024, 7, 1, 3, 30, tzinfo=dt.timezone.utc)    # Mon
    assert au._in_window(sunday_3am) is True
    assert au._in_window(sunday_6am) is False
    assert au._in_window(monday_3am) is False
