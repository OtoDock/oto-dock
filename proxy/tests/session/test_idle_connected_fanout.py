"""Connected-but-idle fan-out.

A dashboard edit/upload/delete should reach a CONNECTED satellite that has NO
active session, resolved by its PAIRING (admin-paired ⇒ admin-shared whole folder;
user-paired ⇒ the owner's role-gated scope), so the satellite stays current and its
next session start has little to sync. Active-session machines keep the existing
per-session fan-out (the hot path is untouched: ``include_idle`` defaults False).
"""

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests._paths import PROXY_DIR as _PROXY_DIR
if str(_PROXY_DIR) not in sys.path:
    sys.path.insert(0, str(_PROXY_DIR))


class _Info:
    def __init__(self, machine_id, agent_name, alive=True):
        self.machine_id = machine_id
        self.agent_name = agent_name
        self.alive = alive


class _FakeLayer:
    def __init__(self, sessions, identities):
        self._sessions = sessions          # {sid: _Info}  (active sessions)
        self._identities = identities      # {machine_id: (username|None, role)}

    async def resolve_machine_sync_identity(self, machine_id, agent_slug):
        return self._identities.get(machine_id)


def _setup(monkeypatch, *, sessions, identities, connected, agents_by_machine):
    import core.session.session_manager as sm
    import core.remote.satellite_connection as sc
    import storage.sync_state_store as ss
    layer = _FakeLayer(sessions, identities)
    monkeypatch.setattr(sm, "_get_remote_layer", lambda: layer)
    fake_cm = SimpleNamespace(get_connected_machines=lambda: list(connected))
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)
    monkeypatch.setattr(
        ss, "agents_for_machine",
        lambda mid: set(agents_by_machine.get(mid, ())),
    )


# --- idle_connected_targets: pairing-scoped isolation -----------------------

@pytest.mark.asyncio
async def test_idle_admin_paired_receives_whole_folder(temp_db, monkeypatch):
    # admin-PAIRED idle machine → admin-shared: EVERY user's folder + config.
    _setup(
        monkeypatch,
        sessions={}, identities={"mIdle": (None, "admin")},
        connected=["mIdle"], agents_by_machine={"mIdle": {"agent-1"}},
    )
    from services.remote.workspace_fanout import idle_connected_targets
    assert await idle_connected_targets("agent-1", "users/alice/x.md") == ["mIdle"]
    assert await idle_connected_targets("agent-1", "users/bob/y.md") == ["mIdle"]
    assert await idle_connected_targets("agent-1", "config/p.md") == ["mIdle"]


@pytest.mark.asyncio
async def test_idle_user_paired_is_role_gated(temp_db, monkeypatch):
    # user-paired idle machine owned by alice (editor) → own dir + shared workspace
    # only; NOT another user's dir, NOT config (editor ≠ owner-tier).
    _setup(
        monkeypatch,
        sessions={}, identities={"mIdle": ("alice", "editor")},
        connected=["mIdle"], agents_by_machine={"mIdle": {"agent-1"}},
    )
    from services.remote.workspace_fanout import idle_connected_targets
    assert await idle_connected_targets("agent-1", "users/alice/x.md") == ["mIdle"]
    assert await idle_connected_targets("agent-1", "workspace/w.md") == ["mIdle"]
    assert await idle_connected_targets("agent-1", "users/bob/y.md") == []
    assert await idle_connected_targets("agent-1", "config/p.md") == []


@pytest.mark.asyncio
async def test_idle_excludes_active_and_source(temp_db, monkeypatch):
    # mActive has an active session for agent-1 (covered by the active fan-out);
    # mSrc is the originating machine; only mIdle is a connected-idle target.
    _setup(
        monkeypatch,
        sessions={"s1": _Info("mActive", "agent-1")},
        identities={
            "mActive": (None, "admin"), "mSrc": (None, "admin"),
            "mIdle": (None, "admin"),
        },
        connected=["mActive", "mSrc", "mIdle"],
        agents_by_machine={m: {"agent-1"} for m in ("mActive", "mSrc", "mIdle")},
    )
    from services.remote.workspace_fanout import idle_connected_targets
    out = await idle_connected_targets(
        "agent-1", "workspace/x.md", exclude_machine_id="mSrc",
    )
    assert out == ["mIdle"]


@pytest.mark.asyncio
async def test_idle_skips_machine_not_holding_agent(temp_db, monkeypatch):
    # A connected machine that has never run this agent (no sync_state) must NOT be
    # seeded with a partial tree — its first full sync happens at session start.
    _setup(
        monkeypatch,
        sessions={}, identities={"mIdle": (None, "admin")},
        connected=["mIdle"], agents_by_machine={"mIdle": {"other-agent"}},
    )
    from services.remote.workspace_fanout import idle_connected_targets
    assert await idle_connected_targets("agent-1", "workspace/x.md") == []


# --- has_fanout_candidates: cheap in-memory pre-read gate --------------------

@pytest.mark.asyncio
async def test_has_candidates_detects_idle_only_with_flag(temp_db, monkeypatch):
    _setup(
        monkeypatch,
        sessions={}, identities={"mIdle": (None, "admin")},
        connected=["mIdle"], agents_by_machine={"mIdle": {"agent-1"}},
    )
    from services.remote.workspace_fanout import has_fanout_candidates
    # No active session allowed it; an idle connected machine exists.
    assert has_fanout_candidates("agent-1", "workspace/x.md", include_idle=True) is True
    assert has_fanout_candidates("agent-1", "workspace/x.md", include_idle=False) is False


@pytest.mark.asyncio
async def test_has_candidates_false_when_nothing_connected(temp_db, monkeypatch):
    _setup(
        monkeypatch,
        sessions={}, identities={}, connected=[], agents_by_machine={},
    )
    from services.remote.workspace_fanout import has_fanout_candidates
    assert has_fanout_candidates("agent-1", "workspace/x.md", include_idle=True) is False


# --- fan_out_write: include_idle unions idle targets; default is active-only -

@pytest.mark.asyncio
async def test_fan_out_write_include_idle_unions_idle(temp_db, monkeypatch):
    from services.remote import workspace_fanout
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: [],          # no active targets
    )
    idle_mock = AsyncMock(return_value=["mIdle"])
    monkeypatch.setattr(workspace_fanout, "idle_connected_targets", idle_mock)
    fake_cm = AsyncMock()
    import core.remote.satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)

    await workspace_fanout.fan_out_write(
        "agent-1", "workspace/x.md", b"data", include_idle=True,
    )

    idle_mock.assert_awaited_once()
    assert fake_cm.push_file.await_count == 1
    assert fake_cm.push_file.await_args.args[0] == "mIdle"


@pytest.mark.asyncio
async def test_fan_out_write_default_skips_idle(temp_db, monkeypatch):
    # The per-turn / Collabora / file-tools hot path must NOT resolve idle targets.
    from services.remote import workspace_fanout
    monkeypatch.setattr(
        workspace_fanout, "fanout_targets",
        lambda a, r, *, exclude_machine_id=None: [],
    )
    idle_mock = AsyncMock(return_value=["mIdle"])
    monkeypatch.setattr(workspace_fanout, "idle_connected_targets", idle_mock)
    fake_cm = AsyncMock()
    import core.remote.satellite_connection as sc
    monkeypatch.setattr(sc, "get_connection_manager", lambda: fake_cm)

    await workspace_fanout.fan_out_write("agent-1", "workspace/x.md", b"data")

    idle_mock.assert_not_awaited()        # idle resolution never runs by default
    assert fake_cm.push_file.await_count == 0
