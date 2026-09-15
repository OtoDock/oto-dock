"""Tests for services/infra/quota_monitor.py — threshold evaluation, dedup + hysteresis
re-arm, notification routing, and message formatting."""

import config
import pytest
from services.infra import quota_monitor as qm
from services.infra import storage_quota as sq
from storage.agents import agent_store
from storage import database


def _scope(scope_key="acme:shared", scope_type="shared", agent="acme",
           username=None, owner_sub=None):
    return sq.QuotaScope(scope_key, scope_type, agent, username, (), owner_sub)


# --- dedup state -----------------------------------------------------------

def test_record_and_recorded_thresholds():
    qm._record("k", "bytes", [90, 95])
    assert qm._recorded_thresholds("k", "bytes") == {90, 95}
    qm._record("k", "bytes", [90, 95, 100])  # ON CONFLICT — no dup error
    assert qm._recorded_thresholds("k", "bytes") == {90, 95, 100}
    # metric-scoped: byte alerts don't leak into the inode metric
    assert qm._recorded_thresholds("k", "inodes") == set()


def test_rearm_full_drop_clears_all():
    qm._record("k", "bytes", [90, 95, 100])
    qm._rearm("k", "bytes", 80.0)   # threshold-5 > 80 → 90,95,100 all re-armed
    assert qm._recorded_thresholds("k", "bytes") == set()


def test_rearm_partial_keeps_still_crossed():
    qm._record("k", "bytes", [90, 95, 100])
    qm._rearm("k", "bytes", 91.0)   # only 100 drops far enough below (95 > 91)
    assert qm._recorded_thresholds("k", "bytes") == {90, 95}


def test_rearm_no_drop_keeps_all():
    qm._record("k", "bytes", [90, 95, 100])
    qm._rearm("k", "bytes", 96.0)   # nothing dropped 5% below its threshold
    assert qm._recorded_thresholds("k", "bytes") == {90, 95, 100}


# --- threshold evaluation --------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_fires_only_top_crossed(monkeypatch):
    fired = []

    async def fake_fire(scope, metric, threshold, used, limit):
        fired.append((metric, threshold))

    monkeypatch.setattr(qm, "_fire", fake_fire)
    sc = _scope()
    await qm._evaluate(sc, "bytes", 96, 100)        # 96% → top crossed = 95
    assert fired == [("bytes", 95)]
    # all crossed thresholds recorded so a lower one can't back-fire
    assert qm._recorded_thresholds("acme:shared", "bytes") == {90, 95}

    fired.clear()
    await qm._evaluate(sc, "bytes", 99, 100)        # 95 already recorded, 100 not crossed
    assert fired == []

    await qm._evaluate(sc, "bytes", 100, 100)       # escalates to 100
    assert fired == [("bytes", 100)]
    assert qm._recorded_thresholds("acme:shared", "bytes") == {90, 95, 100}


@pytest.mark.asyncio
async def test_evaluate_rearm_then_refire(monkeypatch):
    fired = []

    async def fake_fire(scope, metric, threshold, used, limit):
        fired.append(threshold)

    monkeypatch.setattr(qm, "_fire", fake_fire)
    sc = _scope()
    await qm._evaluate(sc, "bytes", 96, 100)
    assert fired == [95]
    await qm._evaluate(sc, "bytes", 50, 100)        # drop → re-arm, no fire
    assert fired == [95]
    assert qm._recorded_thresholds("acme:shared", "bytes") == set()
    await qm._evaluate(sc, "bytes", 92, 100)        # climb again → 90 fires
    assert fired == [95, 90]


@pytest.mark.asyncio
async def test_evaluate_below_threshold_never_fires(monkeypatch):
    fired = []
    monkeypatch.setattr(qm, "_fire",
                        lambda *a, **k: fired.append(a) or _noop())
    sc = _scope()
    await qm._evaluate(sc, "bytes", 80, 100)
    assert fired == []


async def _noop():
    return None


# --- notification routing --------------------------------------------------

def test_targets_user_scope_is_owner():
    sc = _scope("user:acme:bob", "user", "acme", "bob", owner_sub="user-bob")
    assert qm._targets_for(sc) == ["user-bob"]


def test_targets_user_scope_without_owner_is_empty():
    sc = _scope("user:acme:bob", "user", "acme", "bob", owner_sub=None)
    assert qm._targets_for(sc) == []


def test_targets_shared_scope_is_managers_and_editors(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    agent_store.create_agent("acme", "Acme")
    database.set_user_agents("user-manager", ["acme"], "user-admin", {"acme": "manager"})
    database.set_user_agents("user-viewer2", ["acme"], "user-admin", {"acme": "editor"})
    database.set_user_agents("user-viewer", ["acme"], "user-admin", {"acme": "viewer"})
    sc = _scope("acme:shared", "shared", "acme")
    targets = set(qm._targets_for(sc))
    assert targets == {"user-manager", "user-viewer2"}  # NOT the viewer


# --- message formatting -----------------------------------------------------

def test_message_full_warns_writes_will_fail():
    sc = _scope()
    title, body = qm._message(sc, "bytes", 100, 100 * 1024 * 1024, 100 * 1024 * 1024)
    assert "full" in title.lower()
    assert "fail" in body.lower()
    assert "acme" in title


def test_message_partial_threshold():
    sc = _scope()
    title, body = qm._message(sc, "bytes", 90, 90 * 1024 * 1024, 100 * 1024 * 1024)
    assert "90%" in title
    assert "%" in body


def test_message_user_scope_names_the_user():
    sc = _scope("user:acme:bob", "user", "acme", "bob", owner_sub="user-bob")
    title, body = qm._message(sc, "bytes", 95, 95 * 1024 * 1024, 100 * 1024 * 1024)
    assert "bob" in body


def test_message_external_scope_names_the_callers():
    sc = _scope("external:acme", "external", "acme")
    title, body = qm._message(sc, "bytes", 100, 1024 ** 3, 1024 ** 3)
    assert "callers' folders" in body
    assert "None" not in body


def test_message_inode_metric():
    sc = _scope()
    title, body = qm._message(sc, "inodes", 100, 10000, 10000)
    assert "file" in body.lower()


def test_fmt_bytes():
    assert qm._fmt_bytes(0) == "0 B"
    assert qm._fmt_bytes(1024).endswith("KB")
    assert qm._fmt_bytes(5 * 1024 * 1024).endswith("MB")
    assert qm._fmt_bytes(3 * 1024 * 1024 * 1024).endswith("GB")


# --- top-level skip ---------------------------------------------------------

@pytest.mark.asyncio
async def test_check_quotas_skips_when_nothing_limited(monkeypatch):
    database.set_platform_setting("quota_shared_folder_mb", "0")
    database.set_platform_setting("quota_user_folder_mb", "0")
    called = {"n": 0}

    def boom():
        called["n"] += 1
        return []

    monkeypatch.setattr(sq, "iter_scopes", boom)
    await qm.check_quotas()
    assert called["n"] == 0  # early-returned before enumerating scopes
