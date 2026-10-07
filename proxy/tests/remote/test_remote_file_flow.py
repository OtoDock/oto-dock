"""Tests for remote_file_flow — workspace-direct pull-through + push-back.

The module no longer maintains a separate `.remote-cache/` cache. pull_through
writes directly into the platform's actual workspace at AGENTS_DIR/<slug>/...
so the dashboard listing reflects the satellite agent's view in real time.

The global per-(agent_slug, rel_path) write lock + the per-session pending_push
write-barrier serialize concurrent pulls/pushes/file_changed-applies — across
sessions and machines.
"""

import asyncio
import dataclasses
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core import placement


class _FakeInfo:
    """Stand-in for RemoteSessionInfo needed by remote_file_flow."""

    def __init__(self, machine_id: str = "m-1", agent_name: str = "agent-1"):
        self.machine_id = machine_id
        self.agent_name = agent_name


@pytest.fixture
def reset_flow():
    """Drop any leaked per-session + global-lock state between tests."""
    from core.remote import remote_file_flow
    remote_file_flow._sessions.clear()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._pull_stat_records.clear()
    remote_file_flow._platform_ahead.clear()
    yield
    remote_file_flow._sessions.clear()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._pull_stat_records.clear()
    remote_file_flow._platform_ahead.clear()


# ---------------------------------------------------------------------------
# pull_through
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_through_returns_none_when_local(temp_db, reset_flow):
    """Local sessions get None back so the caller falls back to local logic."""
    from core.remote import remote_file_flow
    with patch.object(remote_file_flow, "_get_remote_session_info", return_value=None):
        result = await remote_file_flow.pull_through("local-sess", "workspace/foo")
        assert result is None


@pytest.mark.asyncio
async def test_pull_through_writes_to_workspace(temp_db, reset_flow, tmp_path, monkeypatch):
    """First call fetches from satellite and writes to actual workspace."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)

    mock_cm = MagicMock()

    async def fake_pull_to_path(machine_id, ref, dest_path, *, agent_slug="",
                                timeout=180.0):
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"hello-" + ref.value.encode())
        return True

    mock_cm.pull_file_to_path.side_effect = fake_pull_to_path

    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        host = await remote_file_flow.pull_through("sess-1", "workspace/a.txt")
        assert host is not None
        # File lands at the actual workspace path, not a separate cache.
        expected = (tmp_path / "agent-1" / "workspace" / "a.txt").resolve()
        assert host == expected
        assert host.read_bytes() == b"hello-workspace/a.txt"
        assert mock_cm.pull_file_to_path.call_count == 1


@pytest.mark.asyncio
async def test_pull_through_returns_none_when_satellite_fails(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """If the satellite returns no content, no file is written."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)

    mock_cm = MagicMock()

    async def fake_pull_to_path(machine_id, ref, dest_path, *, agent_slug="",
                                timeout=180.0):
        return False

    mock_cm.pull_file_to_path.side_effect = fake_pull_to_path
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        result = await remote_file_flow.pull_through("sess-1", "workspace/a.txt")
        assert result is None
        # No file written
        assert not (tmp_path / "agent-1" / "workspace" / "a.txt").exists()


@pytest.mark.asyncio
async def test_pull_through_blocks_path_traversal(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """Path traversal attempts return None and don't write anywhere."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)

    mock_cm = MagicMock()
    mock_cm.pull_file_to_path = MagicMock()

    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        result = await remote_file_flow.pull_through(
            "sess-1", "../../etc/passwd",
        )
        assert result is None
        # pull_file_to_path shouldn't even be called for traversal attempts
        # since the path check fails before fetching. (Implementation may
        # vary — this is the security guarantee, not the implementation.)


# ---------------------------------------------------------------------------
# push_back
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_push_back_uses_workspace_file(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """push_back reads the platform workspace and forwards to the satellite."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)

    # Pre-populate workspace
    workspace_path = tmp_path / "agent-1" / "workspace" / "out.png"
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_path.write_bytes(b"edited-bytes")

    mock_cm = MagicMock()
    pushed = []

    async def fake_push(machine_id, ref, content, *, agent_slug="", **kwargs):
        pushed.append((machine_id, agent_slug, ref.value, content))
        return True

    mock_cm.push_file.side_effect = fake_push
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        ok = await remote_file_flow.push_back("sess-1", "workspace/out.png")
        assert ok is True
        assert len(pushed) == 1
        assert pushed[0][2] == "workspace/out.png"
        # Streaming refactor (1.4.0): push_back passes the workspace PATH —
        # push_file streams from disk. Verify the path resolves to the bytes.
        from pathlib import Path as _P
        assert _P(pushed[0][3]).read_bytes() == b"edited-bytes"


@pytest.mark.asyncio
async def test_push_back_releases_readers_and_the_path_lock_before_its_fan_out(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """The fan-out to the other machines runs after the path lock is
    released and after the readers of the path were let go (the session's
    own satellite holds the bytes by then), under the fan-out lock alone."""
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    wp = tmp_path / "agent-1" / "workspace" / "out.md"
    wp.parent.mkdir(parents=True, exist_ok=True)
    wp.write_bytes(b"v2")
    seen = {}

    async def fake_fan_out(agent, rel, source, **kw):
        lock = remote_file_flow._global_path_locks[(agent, rel)]
        fan = remote_file_flow._fanout_locks[(agent, rel)]
        st = await remote_file_flow._state("sess-1")
        seen.update(path_lock_free=not lock.locked(), fan_out_held=fan.locked(),
                    readers_released=st.pending_push[rel].is_set())

    monkeypatch.setattr(workspace_fanout, "fan_out_write", fake_fan_out)
    mock_cm = MagicMock()

    async def fake_push(machine_id, ref, content, *, agent_slug="", **kwargs):
        return True

    mock_cm.push_file.side_effect = fake_push
    with patch.object(
        remote_file_flow, "_get_remote_session_info",
        return_value=_FakeInfo(machine_id="m-own", agent_name="agent-1"),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        assert await remote_file_flow.push_back("sess-1", "workspace/out.md") is True
    assert seen == {"path_lock_free": True, "fan_out_held": True, "readers_released": True}
    assert not remote_file_flow._fanout_locks[("agent-1", "workspace/out.md")].locked()


@pytest.mark.asyncio
async def test_push_back_returns_false_when_file_missing(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """No file → push_back is a no-op returning False (not an error)."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)

    mock_cm = MagicMock()
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        ok = await remote_file_flow.push_back("sess-1", "workspace/nope.txt")
        assert ok is False
        mock_cm.push_file.assert_not_called()


@pytest.mark.asyncio
async def test_push_back_fans_out_excluding_own_machine(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """push_back forwards to the session's OWN satellite AND fans the
    same bytes out to every OTHER satellite of the agent (exclude = own machine)."""
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    wp = tmp_path / "agent-1" / "workspace" / "out.md"
    wp.parent.mkdir(parents=True, exist_ok=True)
    wp.write_bytes(b"v2")

    fo = AsyncMock()
    monkeypatch.setattr(workspace_fanout, "fan_out_write", fo)

    mock_cm = MagicMock()

    async def fake_push(machine_id, ref, content, *, agent_slug="", **kwargs):
        return True

    mock_cm.push_file.side_effect = fake_push
    with patch.object(
        remote_file_flow, "_get_remote_session_info",
        return_value=_FakeInfo(machine_id="m-own", agent_name="agent-1"),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        ok = await remote_file_flow.push_back("sess-1", "workspace/out.md")

    assert ok is True
    fo.assert_awaited_once()
    assert fo.await_args.args[:2] == ("agent-1", "workspace/out.md")
    # Fan-out receives the platform PATH (streaming), not bytes.
    from pathlib import Path as _P
    assert _P(fo.await_args.args[2]).read_bytes() == b"v2"
    assert fo.await_args.kwargs.get("exclude_machine_id") == "m-own"


# ---------------------------------------------------------------------------
# cleanup_session
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_drops_session_state(temp_db, reset_flow):
    """cleanup_session drops bookkeeping; it does NOT delete workspace files."""
    from core.remote import remote_file_flow

    # Seed some state
    st = await remote_file_flow._state("sess-1")
    st.pending_push["foo.png"] = asyncio.Event()
    assert "sess-1" in remote_file_flow._sessions

    remote_file_flow.cleanup_session("sess-1")
    assert "sess-1" not in remote_file_flow._sessions


# ---------------------------------------------------------------------------
# Write barrier (concurrent pull during push_back)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_barrier_blocks_reads_during_push(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """A pull_through awaits any pending push_back on the same path."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)

    # Seed workspace so push_back has something to send. Path must be
    # canonical (known top-level scope) — the is_canonical_rel_path gate
    # rejects root-level files.
    workspace_path = tmp_path / "agent-1" / "workspace" / "x.bin"
    workspace_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_path.write_bytes(b"initial")

    slow_push_started = asyncio.Event()
    release_push = asyncio.Event()

    async def slow_push(machine_id, ref, content, *, agent_slug="", **kw):
        slow_push_started.set()
        await release_push.wait()
        return True

    async def fake_pull_to_path(machine_id, ref, dest_path, *, agent_slug="",
                                timeout=180.0):
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"fetched")
        return True

    mock_cm = MagicMock()
    mock_cm.push_file.side_effect = slow_push
    mock_cm.pull_file_to_path.side_effect = fake_pull_to_path

    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        # Start a slow push_back; it holds the barrier.
        push_task = asyncio.create_task(
            remote_file_flow.push_back("sess-1", "workspace/x.bin"),
        )
        await slow_push_started.wait()

        # A pull_through on the same path should block until the push finishes.
        pull_task = asyncio.create_task(
            remote_file_flow.pull_through("sess-1", "workspace/x.bin"),
        )
        # Give the pull a chance to start and be blocked.
        await asyncio.sleep(0.05)
        assert not pull_task.done()

        # Release the push; pull unblocks and returns the workspace path.
        release_push.set()
        await push_task
        result = await asyncio.wait_for(pull_task, timeout=1.0)
        assert result is not None


# ---------------------------------------------------------------------------
# Global per-(agent_slug, rel_path) lock (shared by file_changed + fan-out)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_global_path_lock_same_per_agent_path(temp_db, reset_flow):
    """All writers to one (agent, rel_path) — across sessions/machines — share a
    single global lock; a different file or agent gets a different lock."""
    from core.remote import remote_file_flow
    lock_a1 = await remote_file_flow._acquire_global_path_lock("agent-1", "foo.txt")
    lock_a2 = await remote_file_flow._acquire_global_path_lock("agent-1", "foo.txt")
    lock_b = await remote_file_flow._acquire_global_path_lock("agent-1", "bar.txt")
    lock_c = await remote_file_flow._acquire_global_path_lock("agent-2", "foo.txt")

    assert lock_a1 is lock_a2       # same (agent, path) → same lock
    assert lock_a1 is not lock_b    # different path → different lock
    assert lock_a1 is not lock_c    # different agent → different lock


@pytest.mark.asyncio
async def test_global_lock_serializes_two_sessions_one_file(temp_db, reset_flow):
    """Two distinct writers of the same (agent, file) serialize on the global
    lock — the second can't start until the first releases."""
    from core.remote import remote_file_flow

    held = await remote_file_flow._acquire_global_path_lock("agent-1", "shared.md")
    order: list[str] = []

    async def writer(tag: str):
        lk = await remote_file_flow._acquire_global_path_lock("agent-1", "shared.md")
        async with lk:
            order.append(f"{tag}-start")
            await asyncio.sleep(0.02)
            order.append(f"{tag}-end")

    async with held:
        t1 = asyncio.create_task(writer("A"))
        t2 = asyncio.create_task(writer("B"))
        await asyncio.sleep(0.01)
        assert order == []  # both blocked on the held global lock
    await asyncio.gather(t1, t2)
    # No interleave: each writer's start/end is contiguous.
    assert order in (
        ["A-start", "A-end", "B-start", "B-end"],
        ["B-start", "B-end", "A-start", "A-end"],
    )


# ---------------------------------------------------------------------------
# Post-restart registry fallback (_get_remote_session_info)
# ---------------------------------------------------------------------------


_PLACEMENT = placement.PlacementCapabilities(
    kind=placement.KIND_ADMIN_REMOTE, machine_id="m-1",
    agents_dir="/home/alice/.oto-dock/agents", home_dir="/home/alice",
)


def _remote_ctx(**over):
    """A persisted-shape SecurityContext for a satellite-parented session."""
    from auth.path_policy import SecurityContext
    base = dict(
        role="admin", username="alice", agent="agent-1", is_admin_agent=False,
        placement=_PLACEMENT,
    )
    base.update(over)
    return SecurityContext(**base)


def _empty_layer():
    layer = MagicMock()
    layer._sessions = {}
    return layer


@pytest.fixture
def iso_security(tmp_path, monkeypatch):
    """Isolate the persisted security index + the fallback-log set."""
    from core.remote import remote_file_flow
    from core.session import session_state
    monkeypatch.setattr(
        session_state, "_SECURITY_INDEX", tmp_path / "security_index.json",
    )
    monkeypatch.setattr(session_state, "_session_security", {})
    monkeypatch.setattr(session_state, "_session_security_ts", {})
    remote_file_flow._fallback_logged.clear()
    yield
    remote_file_flow._fallback_logged.clear()


def test_registry_miss_falls_back_to_persisted_ctx(iso_security):
    """A session that survived a proxy restart (registry empty, security ctx
    reloaded from disk) still classifies as remote with the right identity."""
    from core.remote import remote_file_flow
    from core.session import session_state

    session_state.set_session_security(
        "surv-1", _remote_ctx(agent="agent-9", placement=dataclasses.replace(_PLACEMENT, machine_id="m-9")),
    )
    with patch(
        "core.session.session_manager._get_remote_layer",
        return_value=_empty_layer(),
    ):
        info = remote_file_flow._get_remote_session_info("surv-1")
        assert info is not None
        assert info.machine_id == "m-9"
        assert info.agent_name == "agent-9"
        assert remote_file_flow.is_remote_session("surv-1")


def test_registry_miss_without_machine_id_stays_local(iso_security):
    """A local session's ctx (no target_machine_id) never triggers the
    fallback — the hook keeps taking the local branch."""
    from core.remote import remote_file_flow
    from core.session import session_state

    session_state.set_session_security(
        "loc-1",
        _remote_ctx(placement=placement.LOCAL_PLACEMENT),
    )
    with patch(
        "core.session.session_manager._get_remote_layer",
        return_value=_empty_layer(),
    ):
        assert remote_file_flow._get_remote_session_info("loc-1") is None
        assert not remote_file_flow.is_remote_session("loc-1")


def test_registry_miss_without_ctx_stays_local(iso_security):
    from core.remote import remote_file_flow

    with patch(
        "core.session.session_manager._get_remote_layer",
        return_value=_empty_layer(),
    ):
        assert remote_file_flow._get_remote_session_info("ghost") is None


def test_registry_hit_wins_over_fallback(iso_security):
    """A live registry entry is returned as-is (never the shim)."""
    from core.remote import remote_file_flow
    from core.session import session_state

    session_state.set_session_security("live-1", _remote_ctx())
    real = object()
    layer = MagicMock()
    layer._sessions = {"live-1": real}
    with patch(
        "core.session.session_manager._get_remote_layer", return_value=layer,
    ):
        assert remote_file_flow._get_remote_session_info("live-1") is real


def test_closed_session_is_not_resurrected(iso_security):
    """Close pops the security ctx (persisted), so the fallback must not
    re-classify a properly closed session as remote."""
    from core.remote import remote_file_flow
    from core.session import session_state

    session_state.set_session_security("gone-1", _remote_ctx())
    session_state.cleanup_session_permission_state("gone-1")
    with patch(
        "core.session.session_manager._get_remote_layer",
        return_value=_empty_layer(),
    ):
        assert remote_file_flow._get_remote_session_info("gone-1") is None
        assert not remote_file_flow.is_remote_session("gone-1")


@pytest.mark.asyncio
async def test_pull_through_via_fallback_after_restart(
    temp_db, reset_flow, iso_security, tmp_path, monkeypatch,
):
    """End-to-end: registry miss + persisted remote ctx → pull_through still
    fetches from the ctx's machine into the ctx's agent workspace."""
    import config
    from core.remote import remote_file_flow
    from core.session import session_state

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    session_state.set_session_security(
        "surv-2", _remote_ctx(agent="agent-1", placement=dataclasses.replace(_PLACEMENT, machine_id="m-7")),
    )

    mock_cm = MagicMock()

    async def fake_pull_to_path(machine_id, ref, dest_path, *, agent_slug="",
                                timeout=180.0):
        assert machine_id == "m-7"
        assert agent_slug == "agent-1"
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"survivor")
        return True

    mock_cm.pull_file_to_path.side_effect = fake_pull_to_path
    with patch(
        "core.session.session_manager._get_remote_layer",
        return_value=_empty_layer(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager",
        return_value=mock_cm,
    ):
        host = await remote_file_flow.pull_through("surv-2", "workspace/a.txt")
        assert host == (tmp_path / "agent-1" / "workspace" / "a.txt").resolve()
        assert host.read_bytes() == b"survivor"


# ---------------------------------------------------------------------------
# Pull-stat revalidation (file_stat, satellite >= 0.5.95)
# ---------------------------------------------------------------------------


def _mock_cm(*, supports_stat, stat=None, pulled=b"content-v1"):
    """Connection-manager mock: gated stat probe + a pull that writes bytes."""
    cm = MagicMock()
    cm.satellite_supports_file_stat.return_value = supports_stat
    cm.stat_file = AsyncMock(return_value=stat)

    async def fake_pull(machine_id, ref, dest_path, *, agent_slug="",
                        timeout=180.0):
        p = Path(dest_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(pulled)
        return True

    cm.pull_file_to_path = MagicMock(side_effect=fake_pull)
    return cm


_STAT_V1 = {"exists": True, "size": 10, "mtime_ns": 111}
_STAT_V2 = {"exists": True, "size": 12, "mtime_ns": 222}


@pytest.mark.asyncio
async def test_pull_through_fast_path_skips_transfer(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """Second read of an unchanged file: stat matches the pull-time record →
    the workspace copy is served with NO second transfer."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = _mock_cm(supports_stat=True, stat=_STAT_V1)
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        first = await remote_file_flow.pull_through("s1", "workspace/a.txt")
        assert first is not None
        assert cm.pull_file_to_path.call_count == 1

        second = await remote_file_flow.pull_through("s1", "workspace/a.txt")
        assert second == first
        assert cm.pull_file_to_path.call_count == 1  # served from workspace copy
        assert cm.stat_file.await_count == 2


@pytest.mark.asyncio
async def test_pull_through_repulls_on_stat_mismatch(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """A changed satellite file (different stat) always re-transfers."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = _mock_cm(supports_stat=True, stat=_STAT_V1)
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.pull_through("s1", "workspace/a.txt")
        cm.stat_file = AsyncMock(return_value=_STAT_V2)  # file changed
        await remote_file_flow.pull_through("s1", "workspace/a.txt")
        assert cm.pull_file_to_path.call_count == 2


@pytest.mark.asyncio
async def test_pull_through_old_satellite_never_probes(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """Pre-0.5.95 satellite: no stat frames sent (they would be silently
    dropped), every read pulls — the pre-existing behavior."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = _mock_cm(supports_stat=False)
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.pull_through("s1", "workspace/a.txt")
        await remote_file_flow.pull_through("s1", "workspace/a.txt")
        assert cm.pull_file_to_path.call_count == 2
        assert cm.stat_file.await_count == 0


@pytest.mark.asyncio
async def test_pull_through_probe_failure_falls_back_to_pull(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """stat_file returning None (timeout/policy/disconnect) must never serve
    the cache — always fall through to a full pull."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = _mock_cm(supports_stat=True, stat=_STAT_V1)
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.pull_through("s1", "workspace/a.txt")
        cm.stat_file = AsyncMock(return_value=None)  # probe broke
        await remote_file_flow.pull_through("s1", "workspace/a.txt")
        assert cm.pull_file_to_path.call_count == 2


@pytest.mark.asyncio
async def test_push_back_invalidates_stat_record(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """A write-back drops the pull-time record, so the next read re-pulls
    instead of fast-pathing onto a pre-write comparison."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = _mock_cm(supports_stat=True, stat=_STAT_V1)
    cm.push_file = AsyncMock(return_value=True)
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.pull_through("s1", "workspace/a.txt")
        assert remote_file_flow._pull_stat_records  # recorded

        assert await remote_file_flow.push_back("s1", "workspace/a.txt") is True
        assert not remote_file_flow._pull_stat_records  # invalidated

        # Even though the satellite still reports the same stat, the record
        # is gone → full pull.
        await remote_file_flow.pull_through("s1", "workspace/a.txt")
        assert cm.pull_file_to_path.call_count == 2


@pytest.mark.asyncio
async def test_host_path_fast_path_uses_sidecar_stat(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """satellite_host pulls persist the stat in the sidecar; a matching
    probe on the next read serves the session cache without a transfer."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    cm = _mock_cm(supports_stat=True, stat=_STAT_V1)
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        first = await remote_file_flow.pull_through_host_path(
            "s1", "/home/user/Desktop/report.pdf",
        )
        assert first is not None and first.is_file()
        assert cm.pull_file_to_path.call_count == 1

        second = await remote_file_flow.pull_through_host_path(
            "s1", "/home/user/Desktop/report.pdf",
        )
        assert second == first
        assert cm.pull_file_to_path.call_count == 1


@pytest.mark.asyncio
async def test_probe_stat_type_gates_non_dict_replies(reset_flow):
    """Regression: an un-configured mock cm (`AsyncMock().stat_file(...)`
    returns an AsyncMock) flowed straight through `_probe_stat`, and
    `pull_through_host_path` then crashed JSON-serializing it into the cache
    sidecar (`test_satellite_host_paths.py::test_writes_sidecar_with_metadata`).
    The probe's contract is dict-or-None — anything else (malformed ack,
    test double) must degrade to "no probe", never break the read."""
    from core.remote import remote_file_flow

    for bad in (AsyncMock(), "not-a-dict", 42, ["exists"]):
        cm = MagicMock()
        cm.satellite_supports_file_stat.return_value = True
        cm.stat_file = AsyncMock(return_value=bad)
        assert await remote_file_flow._probe_stat(cm, "m-1", object()) is None

    # A genuine dict still passes through untouched.
    cm = _mock_cm(supports_stat=True, stat=_STAT_V1)
    assert await remote_file_flow._probe_stat(cm, "m-1", object()) == _STAT_V1


# ---------------------------------------------------------------------------
# The per-(agent, path) lock map: bounded, never evicting a lock in use
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_lock_map_is_bounded_and_keeps_locks_in_use(reset_flow):
    from core.remote import remote_file_flow as rff
    held = await rff._acquire_global_path_lock("a", "held.txt")
    await held.acquire()
    waited = await rff._acquire_global_path_lock("a", "waited.txt")
    await waited.acquire()
    waiter = asyncio.create_task(waited.acquire())
    await asyncio.sleep(0)
    shared_a = await rff._acquire_global_path_lock("a", "shared.txt")
    for i in range(rff._PATH_LOCKS_MAX + 2000):
        await rff._acquire_global_path_lock("a", f"f{i}.txt")
    assert len(rff._global_path_locks) <= rff._PATH_LOCKS_MAX
    # A lock that is held or awaited is never evicted; a re-acquire hands
    # back the same object.
    assert await rff._acquire_global_path_lock("a", "held.txt") is held
    assert await rff._acquire_global_path_lock("a", "waited.txt") is waited
    # An idle lock that survived the churn is still the same object; one that
    # was evicted comes back fresh, which is safe because nobody was about to
    # enter it.
    again = await rff._acquire_global_path_lock("a", "shared.txt")
    assert again is shared_a or again.idle()
    held.release()
    waited.release()
    await waiter
    waited.release()


@pytest.mark.asyncio
async def test_two_acquirers_of_one_key_share_one_lock(reset_flow):
    from core.remote import remote_file_flow as rff
    a = await rff._acquire_global_path_lock("agent", "x.txt")
    b = await rff._acquire_global_path_lock("agent", "x.txt")
    assert a is b
    fa = await rff.acquire_fanout_lock("agent", "x.txt")
    fb = await rff.acquire_fanout_lock("agent", "x.txt")
    assert fa is fb and fa is not a


def test_every_caller_enters_the_lock_it_was_handed_at_once():
    """The eviction rule leans on this shape: no await between receiving the
    lock and entering it, so an idle lock has no holder about to enter."""
    import re
    from tests._paths import PROXY_DIR
    files = [
        "core/remote/remote_file_flow.py", "core/remote/remote_workspace_sync.py",
        "services/remote/workspace_fanout.py", "core/remote/satellite_file_transfer.py",
    ]
    pattern = re.compile(r"^\s*(\w+) = await (?:remote_file_flow\.)?_acquire_global_path_lock\(")
    seen = 0
    for rel in files:
        lines = (PROXY_DIR / rel).read_text().splitlines()
        for idx, line in enumerate(lines):
            m = pattern.match(line)
            if not m:
                continue
            seen += 1
            name = m.group(1)
            window = lines[idx + 1: idx + 6]
            entered = False
            for follow in window:
                if f"async with {name}" in follow:
                    entered = True
                    break
                assert "await" not in follow, f"{rel}:{idx + 1}: an await before entering {name}"
            assert entered, f"{rel}:{idx + 1}: {name} is not entered within five lines"
    assert seen >= 6


@pytest.mark.asyncio
async def test_push_back_cancelled_in_the_fan_out_wait_releases_nobodys_lock(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """A push_back cancelled while waiting for the fan-out lock another
    writer holds must leave that writer's lock held (asyncio's release
    has no owner check)."""
    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    wp = tmp_path / "agent-1" / "workspace" / "out.md"
    wp.parent.mkdir(parents=True, exist_ok=True)
    wp.write_bytes(b"v2")
    other = await remote_file_flow.acquire_fanout_lock("agent-1", "workspace/out.md")
    await other.acquire()                 # the applier, mid fan-out
    mock_cm = MagicMock()

    async def fake_push(machine_id, ref, content, *, agent_slug="", **kwargs):
        return True

    mock_cm.push_file.side_effect = fake_push
    with patch.object(
        remote_file_flow, "_get_remote_session_info",
        return_value=_FakeInfo(machine_id="m-own", agent_name="agent-1"),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        task = asyncio.create_task(remote_file_flow.push_back("sess-1", "workspace/out.md"))
        for _ in range(20):
            await asyncio.sleep(0.01)
            if other._waiters:            # parked on the fan-out lock
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert other.locked()                 # the applier still holds it
    other.release()


# ---------------------------------------------------------------------------
# The platform-ahead marker: a failed push never lets a read revert the write
# ---------------------------------------------------------------------------


def _ahead_rig(monkeypatch, tmp_path, *, push_ok, stat=_STAT_V1, machine="m-1"):
    """The agent's file-tools write on the platform after a pull recorded
    the machine's copy: a cm whose push answers ``push_ok`` and whose pull
    would bring the machine's older bytes."""
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    monkeypatch.setattr(workspace_fanout, "fan_out_write", AsyncMock())
    target = tmp_path / "agent-1" / "workspace" / "r.docx"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"the agent's new bytes")
    remote_file_flow._record_agent_stat((machine, "agent-1", "workspace/r.docx"), dict(stat))
    cm = _mock_cm(supports_stat=True, stat=stat, pulled=b"the machine's old bytes")
    cm.push_file = AsyncMock(return_value=push_ok)
    return cm, target


async def _repushes():
    """The re-pushes reads started, run to their end."""
    from core.remote import remote_file_flow
    await asyncio.gather(*list(remote_file_flow._repush_tasks))


def _as(machine):
    """A session on ``machine``."""
    from core.remote import remote_file_flow
    return patch.object(remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(machine_id=machine))


@pytest.mark.asyncio
async def test_a_failed_push_back_marks_the_platform_ahead_on_that_machine(
    temp_db, reset_flow, tmp_path, monkeypatch, caplog,
):
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    cm.stat_file = AsyncMock(return_value=dict(_STAT_V2))  # differs from the record
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ), caplog.at_level("WARNING", logger="claude-proxy"):
        assert await remote_file_flow.push_back("s1", "workspace/r.docx") is False
    st = target.stat()
    assert list(remote_file_flow._platform_ahead) == [("m-1", "agent-1", "workspace/r.docx")]
    marker = remote_file_flow._platform_ahead[("m-1", "agent-1", "workspace/r.docx")]
    # The baseline is probed at the failure and wins over the record (a
    # record can be older than a change applied since).
    assert (marker["size"], marker["mtime_ns"], marker["machine"]) == (st.st_size, st.st_mtime_ns, _STAT_V2)
    assert marker["in_flight_until"] == 0.0
    assert "did not reach machine m-1" in caplog.text
    # The baseline is probed at the failure (a record can be older than a
    # change applied since): here the machine answered _STAT_V1.
    assert cm.stat_file.await_args.kwargs["timeout"] == remote_file_flow._BASELINE_PROBE_S


@pytest.mark.asyncio
async def test_a_failure_the_machine_does_not_answer_falls_back_to_the_record(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    from core.remote import remote_file_flow
    cm, _target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    cm.stat_file = AsyncMock(return_value=None)
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.push_back("s1", "workspace/r.docx")
    assert remote_file_flow._platform_ahead[("m-1", "agent-1", "workspace/r.docx")]["machine"] == _STAT_V1


@pytest.mark.asyncio
async def test_an_acked_push_back_keeps_the_other_machines_records(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    # Until the fan-out lands there, a read on another machine still matches
    # its older copy and is served the platform's, never pulls it.
    from core.remote import remote_file_flow
    cm, _target = _ahead_rig(monkeypatch, tmp_path, push_ok=True)
    remote_file_flow._record_agent_stat(("m-2", "agent-1", "workspace/r.docx"), dict(_STAT_V2))
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        assert await remote_file_flow.push_back("s1", "workspace/r.docx") is True
    assert ("m-1", "agent-1", "workspace/r.docx") not in remote_file_flow._pull_stat_records
    assert remote_file_flow._pull_stat_records[("m-2", "agent-1", "workspace/r.docx")] == _STAT_V2


@pytest.mark.asyncio
async def test_a_failure_with_no_record_probes_the_machine_once_for_the_baseline(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    remote_file_flow._pull_stat_records.clear()
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.push_back("s1", "workspace/r.docx")
        marker = remote_file_flow._platform_ahead[("m-1", "agent-1", "workspace/r.docx")]
        assert marker["machine"] == _STAT_V1
        assert cm.stat_file.await_args.kwargs["timeout"] == remote_file_flow._BASELINE_PROBE_S
        # A native edit after it (the probe moves) wins at the next read.
        cm.stat_file = AsyncMock(return_value=_STAT_V2)
        await remote_file_flow.pull_through("s1", "workspace/r.docx")
    assert cm.pull_file_to_path.call_count == 1
    assert target.read_bytes() == b"the machine's old bytes"


@pytest.mark.asyncio
async def test_a_push_in_flight_is_served_from_the_platform_without_a_probe(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=True)
    st = target.stat()
    remote_file_flow.note_platform_ahead("m-1", "agent-1", "workspace/r.docx",
                                         st.st_size, st.st_mtime_ns, in_flight_s=60)
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        assert await remote_file_flow.pull_through("s1", "workspace/r.docx") == target.resolve()
    assert target.read_bytes() == b"the agent's new bytes"
    cm.stat_file.assert_not_awaited()
    cm.push_file.assert_not_awaited()
    assert cm.pull_file_to_path.call_count == 0


@pytest.mark.asyncio
async def test_a_failing_re_push_waits_its_backoff(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.push_back("s1", "workspace/r.docx")
        await remote_file_flow.pull_through("s1", "workspace/r.docx")
        await _repushes()
        for _ in range(2):
            await remote_file_flow.pull_through("s1", "workspace/r.docx")
        await _repushes()
        # The failed push_back, then one re-push: the next reads wait.
        assert cm.push_file.await_count == 2
        remote_file_flow._platform_ahead[("m-1", "agent-1", "workspace/r.docx")]["retry_at"] = 0.0
        await remote_file_flow.pull_through("s1", "workspace/r.docx")
        await _repushes()
        assert cm.push_file.await_count == 3
    assert target.read_bytes() == b"the agent's new bytes"


@pytest.mark.asyncio
async def test_a_read_after_the_failed_push_keeps_the_platform_copy_and_pushes_again(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.push_back("s1", "workspace/r.docx")
        # The machine is still behind (its probe matches the last pull) and
        # still unreachable: the read serves the platform copy.
        assert await remote_file_flow.pull_through("s1", "workspace/r.docx") == target.resolve()
        await _repushes()
        assert target.read_bytes() == b"the agent's new bytes"
        assert cm.pull_file_to_path.call_count == 0
        assert cm.push_file.await_count == 2
        assert remote_file_flow._platform_ahead
        # Back online: after the read the bytes go there, under the path
        # lock the push takes itself, and the marker goes.
        remote_file_flow._platform_ahead[("m-1", "agent-1", "workspace/r.docx")]["retry_at"] = 0.0
        cm.push_file = AsyncMock(return_value=True)
        assert await remote_file_flow.pull_through("s1", "workspace/r.docx") == target.resolve()
        await _repushes()
        cm.push_file.assert_awaited_once()
        assert cm.push_file.await_args.args[0] == "m-1"
        assert cm.pull_file_to_path.call_count == 0
        assert not remote_file_flow._platform_ahead
        assert target.read_bytes() == b"the agent's new bytes"


@pytest.mark.asyncio
async def test_a_read_never_waits_for_the_re_push(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """On a slow link the re-push may take minutes: the read answers with
    the platform copy at once, and reads meanwhile serve it unprobed."""
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    release = asyncio.Event()

    async def _slow_push(*_a, **_k):
        await release.wait()
        return True

    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.push_back("s1", "workspace/r.docx")
        remote_file_flow._platform_ahead[("m-1", "agent-1", "workspace/r.docx")]["retry_at"] = 0.0
        cm.push_file = AsyncMock(side_effect=_slow_push)
        probes = cm.stat_file.await_count
        got = await asyncio.wait_for(remote_file_flow.pull_through("s1", "workspace/r.docx"), 2)
        assert got == target.resolve()
        # The re-push now holds the path lock for as long as the link takes.
        lock = remote_file_flow._global_path_locks[("agent-1", "workspace/r.docx")]
        while not lock.locked():
            await asyncio.sleep(0)
        assert await asyncio.wait_for(remote_file_flow.pull_through("s1", "workspace/r.docx"), 2) == got
        assert cm.stat_file.await_count == probes + 1     # the second read is in flight
        release.set()
        await _repushes()
    assert not remote_file_flow._platform_ahead
    assert cm.push_file.await_count == 1


@pytest.mark.asyncio
async def test_a_re_push_that_raises_backs_off_like_one_that_fails(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.push_back("s1", "workspace/r.docx")
        key = ("m-1", "agent-1", "workspace/r.docx")
        remote_file_flow._platform_ahead[key]["retry_at"] = 0.0
        cm.push_file = AsyncMock(side_effect=OSError("EIO"))
        await remote_file_flow.pull_through("s1", "workspace/r.docx")
        await _repushes()
        marker = remote_file_flow._platform_ahead[key]
        assert marker["in_flight_until"] == 0.0           # not served unprobed for 10 min
        assert marker["retry_at"] > time.monotonic() + remote_file_flow._REPUSH_BACKOFF_S - 5


@pytest.mark.asyncio
async def test_the_marker_is_read_only_on_its_own_machine(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    from core.remote import remote_file_flow
    cm, _target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    with patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        with _as("m-1"):
            await remote_file_flow.push_back("s1", "workspace/r.docx")
        # A session on another machine, where the fan-out landed, pulls its
        # own copy as before.
        cm.stat_file = AsyncMock(return_value=_STAT_V2)
        with _as("m-2"):
            await remote_file_flow.pull_through("s2", "workspace/r.docx")
        assert cm.pull_file_to_path.call_count == 1
        assert ("m-1", "agent-1", "workspace/r.docx") in remote_file_flow._platform_ahead


@pytest.mark.asyncio
async def test_a_machine_changed_after_the_failed_push_wins(
    temp_db, reset_flow, tmp_path, monkeypatch, caplog,
):
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.push_back("s1", "workspace/r.docx")
        cm.stat_file = AsyncMock(return_value=_STAT_V2)  # a native edit there
        with caplog.at_level("WARNING", logger="claude-proxy.remote-file-flow"):
            await remote_file_flow.pull_through("s1", "workspace/r.docx")
    assert cm.pull_file_to_path.call_count == 1
    assert target.read_bytes() == b"the machine's old bytes"
    assert not remote_file_flow._platform_ahead
    assert "the machine's copy is taken" in caplog.text


@pytest.mark.asyncio
async def test_a_later_platform_write_or_push_drops_the_marker(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    import os
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    key = ("m-1", "agent-1", "workspace/r.docx")
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.push_back("s1", "workspace/r.docx")
        assert key in remote_file_flow._platform_ahead
        cm.push_file = AsyncMock(return_value=True)
        assert await remote_file_flow.push_back("s1", "workspace/r.docx") is True
        assert key not in remote_file_flow._platform_ahead
        cm.push_file = AsyncMock(return_value=False)
        await remote_file_flow.push_back("s1", "workspace/r.docx")
        assert key in remote_file_flow._platform_ahead
        # A platform write since (a pull, a save) is the copy's news now.
        target.write_bytes(b"written again")
        st = target.stat()
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
        remote_file_flow._record_agent_stat(key, dict(_STAT_V1))
        await remote_file_flow.pull_through("s1", "workspace/r.docx")
        assert key not in remote_file_flow._platform_ahead

