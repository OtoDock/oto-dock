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


@pytest.fixture(autouse=True)
def _write_back_open(request, monkeypatch):
    """The transfer tests run as a session that may write the path back;
    ``TestWriteBackGate`` judges the gate itself on real contexts."""
    if getattr(request.cls, "real_write_gate", False):
        return
    from core.remote import remote_file_flow
    monkeypatch.setattr(remote_file_flow, "write_back_allowed", AsyncMock(return_value=True))


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
        p.write_bytes(b"hello-" + ref.value.encode())  # noqa: ASYNC240  # a test's own temporary files
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
        from pathlib import Path as _P
        # push_back passes a PATH push_file streams from (the descriptor it
        # holds open for the push).
        pushed.append((machine_id, agent_slug, ref.value, _P(content).read_bytes()))  # noqa: ASYNC240  # a test's own temporary files
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
        assert pushed[0][3] == b"edited-bytes"


@pytest.mark.asyncio
async def test_push_back_never_follows_a_link_swapped_in_while_it_waited(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """The hook judged a regular file; while the push waited on the path
    lock, the name (or a folder above it) became a link to another
    person's file. The push opens the path as written, under the lock, with
    no component followed: nothing is sent."""
    import os

    import config
    from core.remote import remote_file_flow

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    mine = tmp_path / "agent-1" / "users" / "alice" / "workspace"
    theirs = tmp_path / "agent-1" / "users" / "bob" / "workspace"
    mine.mkdir(parents=True)
    theirs.mkdir(parents=True)
    (theirs / "secret.txt").write_bytes(b"bob's")
    pushed = []

    async def fake_push(machine_id, ref, content, *, agent_slug="", **kwargs):
        pushed.append(ref.value)
        return True

    mock_cm = MagicMock()
    mock_cm.push_file.side_effect = fake_push
    with patch.object(
        remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=mock_cm,
    ):
        os.symlink(theirs / "secret.txt", mine / "l.txt")
        assert await remote_file_flow.push_back("sess-1", "users/alice/workspace/l.txt") is False
        (mine / "via").symlink_to(theirs)
        assert await remote_file_flow.push_back(
            "sess-1", "users/alice/workspace/via/secret.txt") is False
        (mine / "plain.txt").write_bytes(b"alice's")
        assert await remote_file_flow.push_back("sess-1", "users/alice/workspace/plain.txt") is True
    assert pushed == ["users/alice/workspace/plain.txt"]


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
    assert _P(fo.await_args.args[2]).read_bytes() == b"v2"  # noqa: ASYNC240  # a test's own temporary files
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
        p.write_bytes(b"fetched")  # noqa: ASYNC240  # a test's own temporary files
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
        p.write_bytes(b"survivor")  # noqa: ASYNC240  # a test's own temporary files
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
        p.write_bytes(pulled)  # noqa: ASYNC240  # a test's own temporary files
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
    ), caplog.at_level("WARNING", logger="otodock"):
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
async def test_a_save_the_machine_refused_as_stale_is_kept_when_its_copy_wins(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """The platform copy is a save the machine refused (it was changing the
    file); the machine's copy then moved on. The read takes the machine's
    copy, and the save goes to the recover bin first, as a conflict."""
    from core.remote import remote_file_flow
    from storage.files import recover_bin_store
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    st = target.stat()
    remote_file_flow.note_platform_ahead("m-1", "agent-1", "workspace/r.docx",
                                         st.st_size, st.st_mtime_ns,
                                         machine=dict(_STAT_V1), stale=True)
    assert remote_file_flow.stale_hold("m-1", "agent-1", "workspace/r.docx")
    captured: list = []
    monkeypatch.setattr(
        recover_bin_store, "capture",
        lambda agent, rp, data, reason: captured.append((agent, rp, data, reason)) or {"entry_id": "e1"})
    cm.stat_file = AsyncMock(return_value=dict(_STAT_V2))   # the machine's copy moved on
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.pull_through("s1", "workspace/r.docx")
    assert captured == [("agent-1", "workspace/r.docx", b"the agent's new bytes", "conflict")]
    assert target.read_bytes() == b"the machine's old bytes"
    assert not remote_file_flow.stale_hold("m-1", "agent-1", "workspace/r.docx")
    assert ("m-1", "agent-1", "workspace/r.docx") not in remote_file_flow._platform_ahead


@pytest.mark.asyncio
async def test_a_copy_a_failed_push_left_behind_is_taken_without_a_capture(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """A push that did not reach the machine (no refusal) leaves the mark
    without the stale flag: the later machine copy is taken as before."""
    from core.remote import remote_file_flow
    from storage.files import recover_bin_store
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    st = target.stat()
    remote_file_flow.note_platform_ahead("m-1", "agent-1", "workspace/r.docx",
                                         st.st_size, st.st_mtime_ns, machine=dict(_STAT_V1))
    assert not remote_file_flow.stale_hold("m-1", "agent-1", "workspace/r.docx")
    monkeypatch.setattr(recover_bin_store, "capture",
                        lambda *a, **kw: pytest.fail("nothing is captured"))
    cm.stat_file = AsyncMock(return_value=dict(_STAT_V2))
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.pull_through("s1", "workspace/r.docx")
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
        while not lock.locked():  # noqa: ASYNC110  # a lock's state: asyncio.Lock has no acquired event
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
        with caplog.at_level("WARNING", logger="otodock.remote-file-flow"):
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



@pytest.mark.asyncio
async def test_push_back_marks_its_targets_before_it_releases_the_path_lock(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """From the path lock's release until the fan-out marks its targets
    itself, a read there with no pull record would pull the older bytes
    back: push_back marks them inside the lock and hands the marks on."""
    import config
    from core.remote import remote_file_flow
    from services.remote import workspace_fanout

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    wp = tmp_path / "agent-1" / "workspace" / "out.md"
    wp.parent.mkdir(parents=True, exist_ok=True)
    wp.write_bytes(b"v2")
    seen = {}

    async def _mark(agent, rel, *, exclude_machine_id=None):
        seen["path_lock_held"] = remote_file_flow._global_path_locks[(agent, rel)].locked()
        seen["exclude"] = exclude_machine_id
        return {"m-other": (2, 7)}

    fo = AsyncMock()
    monkeypatch.setattr(workspace_fanout, "mark_fan_out_targets", _mark)
    monkeypatch.setattr(workspace_fanout, "fan_out_write", fo)
    cm = MagicMock()
    cm.push_file = AsyncMock(return_value=True)
    with patch.object(
        remote_file_flow, "_get_remote_session_info",
        return_value=_FakeInfo(machine_id="m-own", agent_name="agent-1"),
    ), patch("core.remote.satellite_connection.get_connection_manager", return_value=cm):
        assert await remote_file_flow.push_back("sess-1", "workspace/out.md") is True
    assert seen == {"path_lock_held": True, "exclude": "m-own"}
    assert fo.await_args.kwargs["marked"] == {"m-other": (2, 7)}


@pytest.mark.asyncio
async def test_the_re_push_renews_its_window_inside_the_transfer_gate(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """The read's re-push takes a transfer slot and marks the machine in
    flight again there (the clock counts from the slot, not the queue),
    keeping the mark's baseline."""
    from core.remote import remote_file_flow, transfer_gate
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    key = ("m-1", "agent-1", "workspace/r.docx")
    seen = {}
    real_slot = transfer_gate.slot

    def _slot(machine_id, agent, rel, size, **kw):
        seen["slot"] = (machine_id, agent, rel)
        return real_slot(machine_id, agent, rel, size, **kw)

    async def _push(machine_id, ref, source, **kw):
        marker = remote_file_flow._platform_ahead[key]
        seen["in_flight_left"] = marker["in_flight_until"] - time.monotonic()
        seen["machine"] = marker["machine"]
        return True

    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        await remote_file_flow.push_back("s1", "workspace/r.docx")
        remote_file_flow._platform_ahead[key]["retry_at"] = 0.0
        baseline = remote_file_flow._platform_ahead[key]["machine"]
        monkeypatch.setattr(transfer_gate, "slot", _slot)
        cm.push_file = AsyncMock(side_effect=_push)
        await remote_file_flow.pull_through("s1", "workspace/r.docx")
        await _repushes()
    assert seen["slot"] == ("m-1", "agent-1", "workspace/r.docx")
    assert seen["in_flight_left"] > 500       # push_ceiling_s, renewed at the slot
    assert seen["machine"] == baseline
    assert key not in remote_file_flow._platform_ahead
    assert target.read_bytes() == b"the agent's new bytes"


@pytest.mark.asyncio
async def test_an_older_push_landing_under_a_newer_mark_drops_its_baseline(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """A fan-out of older bytes acks (or fails: an ack can be lost) while a
    newer write's mark stands: the machine may hold the older bytes now, so
    the newer mark keeps no baseline and a read there pushes the platform
    copy again instead of taking the machine's copy as a later edit."""
    import config
    from core.remote import remote_file_flow
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    target = tmp_path / "agent-1" / "workspace" / "r.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"the newer write")
    st = target.stat()
    key = ("m-1", "agent-1", "workspace/r.docx")
    remote_file_flow._record_agent_stat(key, dict(_STAT_V1))
    remote_file_flow.note_platform_ahead(*key, st.st_size, st.st_mtime_ns, in_flight_s=60)
    assert remote_file_flow._platform_ahead[key]["machine"] == _STAT_V1
    remote_file_flow.clear_platform_ahead(*key, stat=(3, 1))
    assert remote_file_flow._platform_ahead[key]["machine"] is None
    # The newer write's own fan-out re-marks it at its slot: the lost
    # baseline is not restored from the stale record.
    remote_file_flow.note_platform_ahead(*key, st.st_size, st.st_mtime_ns, in_flight_s=60)
    assert remote_file_flow._platform_ahead[key]["machine"] is None
    remote_file_flow._platform_ahead[key]["machine"] = dict(_STAT_V1)
    await remote_file_flow.note_push_failed(MagicMock(), *key, 3, 1)
    assert remote_file_flow._platform_ahead[key]["machine"] is None


def test_a_mark_of_the_same_copy_keeps_its_baseline_and_backoff(reset_flow, tmp_path, monkeypatch):
    import config
    from core.remote import remote_file_flow
    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    key = ("m-1", "agent-1", "workspace/r.docx")
    remote_file_flow.note_platform_ahead(*key, 5, 9, machine=dict(_STAT_V2))
    remote_file_flow._platform_ahead[key]["retry_at"] = 123.0
    remote_file_flow._record_agent_stat(key, dict(_STAT_V1))
    remote_file_flow.note_platform_ahead(*key, 5, 9, in_flight_s=30)
    marker = remote_file_flow._platform_ahead[key]
    assert marker["machine"] == _STAT_V2 and marker["retry_at"] == 123.0
    assert marker["in_flight_until"] > time.monotonic()
    # A probed baseline replaces it; the window ends with a failure's mark.
    remote_file_flow.note_platform_ahead(*key, 5, 9, machine=dict(_STAT_V1))
    assert marker["machine"] == _STAT_V1 and marker["in_flight_until"] == 0.0
    # end_in_flight ends only the window of the copy it names.
    remote_file_flow.note_platform_ahead(*key, 5, 9, in_flight_s=30)
    remote_file_flow.end_in_flight(*key, (6, 9))
    assert marker["in_flight_until"] > 0
    remote_file_flow.end_in_flight(*key, (5, 9))
    assert marker["in_flight_until"] == 0.0


def test_every_fan_out_lock_holder_enters_it_at_once():
    """The fan-out lock map keeps the path lock's eviction rule: no await
    between receiving the lock and entering it."""
    import re

    from tests._paths import PROXY_DIR
    files = [
        "core/remote/remote_file_flow.py", "services/remote/workspace_fanout.py",
        "core/remote/satellite_file_transfer.py",
    ]
    pattern = re.compile(r"^\s*(\w+) = await (?:remote_file_flow\.)?acquire_fanout_lock\(")
    seen = 0
    for rel in files:
        lines = (PROXY_DIR / rel).read_text().splitlines()
        for idx, line in enumerate(lines):
            m = pattern.match(line)
            if not m:
                continue
            seen += 1
            name = m.group(1)
            entered = False
            for follow in lines[idx + 1: idx + 4]:
                if f"async with {name}" in follow or f"await {name}.acquire()" in follow:
                    entered = True
                    break
                assert "await" not in follow, f"{rel}:{idx + 1}: an await before entering {name}"
            assert entered, f"{rel}:{idx + 1}: {name} is not entered within three lines"
    assert seen >= 3


# ---------------------------------------------------------------------------
# The write-back gate of a read-through
# ---------------------------------------------------------------------------


class TestWriteBackGate:
    """A read-through lands the machine's bytes in the platform tree only
    where the turn-end write-back would (``file_sync.can_write_back`` on the
    session's own context). Any other path is never read from the machine:
    the platform's copy, authoritative there, is served when there is one."""

    real_write_gate = True

    @pytest.fixture
    def rig(self, temp_db, reset_flow, tmp_path, monkeypatch):
        import types

        import config
        from core.remote import remote_file_flow
        monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
        cm = _mock_cm(supports_stat=True, stat=_STAT_V1, pulled=b"machine bytes")
        state: dict = {"ctx": None}
        monkeypatch.setattr(remote_file_flow, "_get_remote_session_info",
                            lambda sid: _FakeInfo())
        monkeypatch.setattr("core.remote.satellite_connection.get_connection_manager",
                            lambda: cm)
        monkeypatch.setattr("core.session.session_state.get_session_security",
                            lambda sid: state["ctx"])
        return types.SimpleNamespace(cm=cm, state=state, agent_dir=tmp_path / "agent-1",
                                     flow=remote_file_flow)

    def _platform_file(self, rig, rel: str) -> Path:
        target = rig.agent_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"platform bytes")
        return target

    def _assert_held(self, rig, target: Path, held: list[str], rel: str) -> None:
        assert target.read_bytes() == b"platform bytes" and held == [rel]
        rig.cm.pull_file_to_path.assert_not_called()
        rig.cm.stat_file.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("role,rel,pulls", [
        ("viewer", "knowledge/notes.md", False),
        ("contributor", "knowledge/notes.md", False),
        ("editor", "knowledge/notes.md", False),
        ("manager", "knowledge/notes.md", True),
        ("viewer", "workspace/a.txt", False),
        ("contributor", "workspace/a.txt", True),
        ("viewer", "users/alice/workspace/a.txt", True),
        ("admin", "users/bob/workspace/a.txt", False),
        ("editor", "config/agent.md", False),
        ("manager", "config/agent.md", True),
        ("admin", "users/alice/.codex/config.toml", False),
    ])
    async def test_the_session_role_decides(self, rig, role, rel, pulls):
        rig.state["ctx"] = _remote_ctx(role=role)
        target = self._platform_file(rig, rel)
        held: list[str] = []
        got = await rig.flow.pull_through("s1", rel, held=held)
        assert got == target.resolve()
        if pulls:
            assert target.read_bytes() == b"machine bytes" and held == []
        else:
            self._assert_held(rig, target, held, rel)

    @pytest.mark.asyncio
    async def test_a_held_path_the_platform_lacks_is_none_and_creates_nothing(self, rig):
        rig.state["ctx"] = _remote_ctx(role="viewer")
        held: list[str] = []
        assert await rig.flow.pull_through("s1", "knowledge/planted.md", held=held) is None
        assert held == ["knowledge/planted.md"]
        assert not (rig.agent_dir / "knowledge").exists()
        rig.cm.pull_file_to_path.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_service_session_reaches_knowledge_only_with_manager_provenance(self, rig):
        rel = "knowledge/notes.md"
        target = self._platform_file(rig, rel)
        rig.state["ctx"] = _remote_ctx(role="manager", username="")
        held: list[str] = []
        await rig.flow.pull_through("s1", rel, held=held)
        self._assert_held(rig, target, held, rel)
        rig.state["ctx"] = _remote_ctx(role="manager", username="", knowledge_rw=True)
        await rig.flow.pull_through("s1", rel)
        assert target.read_bytes() == b"machine bytes"

    @pytest.mark.asyncio
    async def test_a_library_mirror_needs_a_writable_attachment(self, rig, monkeypatch):
        from storage.knowledge import db_knowledge_libraries
        rel = "knowledge/shared/src-agent/guide.md"
        target = self._platform_file(rig, rel)
        rig.state["ctx"] = _remote_ctx(role="manager")
        pairs: dict = {"now": frozenset()}
        monkeypatch.setattr(db_knowledge_libraries, "writable_pairs_for",
                            lambda agent: pairs["now"])
        held: list[str] = []
        await rig.flow.pull_through("s1", rel, held=held)
        self._assert_held(rig, target, held, rel)
        pairs["now"] = frozenset({("src-agent", "")})
        await rig.flow.pull_through("s1", rel)
        assert target.read_bytes() == b"machine bytes"

    @pytest.mark.asyncio
    async def test_a_link_in_the_platform_tree_never_carries_a_pull_out(self, rig):
        """The gate judges the path as written (the session's own folder),
        so the pull is handed the path as written: a link planted there
        toward knowledge/ (a local session of the agent can make one) refuses
        the pull, and knowledge/ keeps its bytes."""
        import os

        from core.remote.satellite_file_transfer import SatelliteFileTransferMixin
        rig.state["ctx"] = _remote_ctx(role="viewer")
        knowledge = rig.agent_dir / "knowledge"
        knowledge.mkdir(parents=True)
        (knowledge / "x.md").write_bytes(b"platform knowledge")
        ws = rig.agent_dir / "users" / "alice" / "workspace"
        ws.mkdir(parents=True)
        (ws / "kn").symlink_to("../../../knowledge")

        async def pull(machine_id, ref, dest, *, agent_slug="", **kw):
            # The real destination open: beneath the agents root, no
            # component followed.
            try:
                rootfd, _rel = SatelliteFileTransferMixin._open_pull_root(Path(dest))
            except OSError:
                return False
            os.close(rootfd)
            Path(dest).write_bytes(b"machine bytes")  # noqa: ASYNC240  # a test's own temporary files
            return True

        rig.cm.pull_file_to_path = MagicMock(side_effect=pull)
        await rig.flow.pull_through("s1", "users/alice/workspace/kn/x.md")
        assert rig.cm.pull_file_to_path.call_args.args[2] == ws / "kn" / "x.md"
        assert (knowledge / "x.md").read_bytes() == b"platform knowledge"

    @pytest.mark.asyncio
    async def test_the_gate_is_judged_again_right_before_the_pull(self, rig, monkeypatch):
        """A library attachment can turn read-only while a read waits for the
        path lock or the probe: the second verdict holds the path."""
        rel = "workspace/a.txt"
        target = self._platform_file(rig, rel)
        monkeypatch.setattr(rig.flow, "write_back_allowed", AsyncMock(side_effect=[True, False]))
        held: list[str] = []
        got = await rig.flow.pull_through("s1", rel, held=held)
        assert got == target and target.read_bytes() == b"platform bytes" and held == [rel]
        rig.cm.pull_file_to_path.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_held_path_serves_no_link(self, rig):
        """The held branch serves a regular file reached with no link: a
        link at the path (to another user's file) is served as nothing."""
        rig.state["ctx"] = _remote_ctx(role="viewer")
        secret = rig.agent_dir / "users" / "bob" / "workspace" / "secret.md"
        secret.parent.mkdir(parents=True)
        secret.write_bytes(b"bob's")
        (rig.agent_dir / "knowledge").mkdir(parents=True)
        (rig.agent_dir / "knowledge" / "notes.md").symlink_to(secret)
        assert await rig.flow.pull_through("s1", "knowledge/notes.md") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ctx", [None, "other-agent"])
    async def test_no_context_or_another_agents_is_held(self, rig, ctx):
        rig.state["ctx"] = None if ctx is None else _remote_ctx(agent=ctx)
        rel = "workspace/a.txt"
        target = self._platform_file(rig, rel)
        held: list[str] = []
        await rig.flow.pull_through("s1", rel, held=held)
        self._assert_held(rig, target, held, rel)


@pytest.mark.asyncio
async def test_a_reads_re_push_sends_the_platform_copy_it_opened_never_a_link(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    """The re-push a read starts opens the platform copy under the path lock
    with no component followed and pushes from that descriptor: a link
    swapped in at the name sends nothing and drops the marker."""
    import os

    import config
    from core.remote import remote_file_flow
    from services.path_policy_v2 import PathRef

    monkeypatch.setattr(config, "AGENTS_DIR", tmp_path)
    ws = tmp_path / "agent-1" / "users" / "alice" / "workspace"
    ws.mkdir(parents=True)
    other = tmp_path / "agent-1" / "users" / "bob" / "workspace" / "secret.txt"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"bob's")
    rel = "users/alice/workspace/doc.txt"
    host = tmp_path / "agent-1" / rel
    pushed = []

    class _CM:
        async def push_file(self, machine_id, ref, content, *, agent_slug="", **kw):
            from pathlib import Path as _P
            pushed.append(_P(content).read_bytes())  # noqa: ASYNC240  # a test's own temporary files
            return True

    async def run():
        st = host.lstat()
        key = ("m1", "agent-1", rel)
        remote_file_flow.note_platform_ahead("m1", "agent-1", rel, st.st_size, st.st_mtime_ns)
        await remote_file_flow._repush(_CM(), "m1", "agent-1", rel,
                                       PathRef("agent_tree", rel), host, key)
        return key

    host.write_bytes(b"alice's")
    key = await run()
    assert pushed == [b"alice's"] and key not in remote_file_flow._platform_ahead
    host.unlink()
    os.symlink(other, host)
    key = await run()
    assert pushed == [b"alice's"] and key not in remote_file_flow._platform_ahead


@pytest.mark.asyncio
async def test_a_push_back_expects_the_recorded_copy_and_a_stale_answer_marks_it_ahead(
    temp_db, reset_flow, tmp_path, monkeypatch, caplog,
):
    """The push carries the machine's recorded stat (taken before the push);
    a stale answer marks the platform copy ahead with the ack's stat as the
    baseline, with no probe, and the record is gone until a pull or a push
    lands."""
    from core.remote import remote_file_flow
    cm, target = _ahead_rig(monkeypatch, tmp_path, push_ok=False)
    seen: dict = {}

    async def _push(machine_id, ref, content, *, agent_slug="", expect=None, stale_out=None, **kw):
        seen["expect"] = expect
        seen["record_during"] = ("m-1", "agent-1", "workspace/r.docx") in remote_file_flow._pull_stat_records
        stale_out.update(reason="changed", size=12, mtime_ns=222)
        return False
    cm.push_file = _push
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ), caplog.at_level("INFO", logger="otodock"):
        assert await remote_file_flow.push_back("s1", "workspace/r.docx") is False
    assert seen == {"expect": {"size": 10, "mtime_ns": 111}, "record_during": False}
    st = target.stat()
    marker = remote_file_flow._platform_ahead[("m-1", "agent-1", "workspace/r.docx")]
    assert (marker["size"], marker["mtime_ns"], marker["machine"]) == (st.st_size, st.st_mtime_ns, _STAT_V2)
    assert "is being changed on machine m-1" in caplog.text
    cm.stat_file.assert_not_awaited()
    assert ("m-1", "agent-1", "workspace/r.docx") not in remote_file_flow._pull_stat_records


@pytest.mark.asyncio
async def test_a_push_back_with_no_record_expects_a_live_file(
    temp_db, reset_flow, tmp_path, monkeypatch,
):
    from core.remote import remote_file_flow
    cm, _target = _ahead_rig(monkeypatch, tmp_path, push_ok=True)
    remote_file_flow._pull_stat_records.clear()
    seen: dict = {}

    async def _push(machine_id, ref, content, *, agent_slug="", expect=None, **kw):
        seen["expect"] = expect
        return True
    cm.push_file = _push
    with _as("m-1"), patch(
        "core.remote.satellite_connection.get_connection_manager", return_value=cm,
    ):
        assert await remote_file_flow.push_back("s1", "workspace/r.docx") is True
    assert seen == {"expect": "live"}


def test_the_expectation_and_the_baseline_of_a_stat_record():
    from core.remote import remote_file_flow
    assert remote_file_flow.push_expectation_of(_STAT_V1) == {"size": 10, "mtime_ns": 111}
    assert remote_file_flow.push_expectation_of(None) == "live"
    assert remote_file_flow.push_expectation_of({"exists": False}) == "live"
    assert remote_file_flow.stale_machine_stat({"reason": "live", "size": 3, "mtime_ns": 9}) == {
        "exists": True, "size": 3, "mtime_ns": 9}
    assert remote_file_flow.stale_machine_stat({"reason": "live", "size": None, "mtime_ns": None}) is None
    remote_file_flow.record_pushed_stat("m-9", "agent-1", "workspace/p.txt", 5, 50)
    try:
        assert remote_file_flow.take_push_record("m-9", "agent-1", "workspace/p.txt") == {
            "exists": True, "size": 5, "mtime_ns": 50}
        assert remote_file_flow.take_push_record("m-9", "agent-1", "workspace/p.txt") is None
    finally:
        remote_file_flow._pull_stat_records.pop(("m-9", "agent-1", "workspace/p.txt"), None)
