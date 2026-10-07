"""Cross-agent file transfer (delegation-mcp ``send_files``).

``services/delegation/file_transfer`` is the single gate + copy engine the
``/v1/delegation/send_files`` endpoint calls. Covers: the spawn-mirroring
authz matrix (kill-switch, source identity, roster edge, target access,
scope clamp, editor tier, daily quota), path validation (traversal,
symlink escape, recursion), copy mechanics (basename landing, structure
preservation, conflict renames, caps), and the audit row. There is
deliberately NO session-start notify (removed 2026-08-14 — the prompt's
workspace listing is the discovery surface).

Remote sources (2026-09-05): ``prefetch_remote_sources`` reads every path
through from the caller's satellite before the copy (same-turn writes,
stale copies, directories via one manifest, the per-call cap, typos that
must not mkdir, offline degradation, invalid paths never reaching the
satellite), and the endpoint's remote-aware 404.

Run: cd proxy && python -m pytest tests/tasks/test_send_files.py -v
"""

import asyncio
import shutil
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import config
from auth.providers import UserContext
from core.remote import remote_file_flow
from services.delegation.file_transfer import (
    MissingSourcePath,
    authorize_send_files,
    perform_send_files,
    prefetch_remote_sources,
    validate_send_path,
)
from storage.agents import agent_store
from storage.files import db_file_transfers
from storage.mcp import mcp_store
from storage import remote_store
from storage import database as task_store


SRC = "xfer-src"
TGT_COLLAB = "xfer-collab"        # collaborative, default user
TGT_SHARED = "xfer-shared"        # shared-only (agent scope only)
TGT_PERSONAL = "xfer-personal"    # personal-only (user scope only)
NO_EDGE = "xfer-no-edge"          # exists, but not on SRC's roster

ALICE = "user-alice"


# ───────────────────────────────────────────────────────────────────────────
# Caller factories (classes mirror test_spawn_authz.py)
# ───────────────────────────────────────────────────────────────────────────


def _user_session(role="editor", *, agents=None, agent_roles=None):
    """Real-user-backed session token minted for SRC."""
    agents = agents if agents is not None else [SRC, TGT_COLLAB, TGT_SHARED, TGT_PERSONAL]
    roles = agent_roles if agent_roles is not None else {a: role for a in agents}
    return UserContext(
        sub=ALICE, email="alice@test.com", name="Alice", role="member",
        agents=agents, agent_roles=roles,
        is_api_key=True, session_id="s-1", agent=SRC,
    )


def _svc_session():
    """No-user service session of SRC (agent-scope task/trigger)."""
    return UserContext(
        sub="session:s-svc", email="session@internal", name="Session Token",
        role="admin", is_api_key=True, session_id="s-svc", agent=SRC,
    )


def _cookie(role="editor"):
    """Dashboard cookie — no agent identity, hence no source tree."""
    return UserContext(
        sub=ALICE, email="alice@test.com", name="Alice", role="member",
        agents=[SRC, TGT_COLLAB], agent_roles={SRC: role, TGT_COLLAB: role},
    )


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def send_env(temp_db, monkeypatch):
    for slug in (SRC, TGT_COLLAB, TGT_SHARED, TGT_PERSONAL, NO_EDGE):
        shutil.rmtree(config.get_agent_dir(slug), ignore_errors=True)
    agent_store.create_agent(SRC, "Src", collaborative=True, default_scope="user")
    agent_store.create_agent(TGT_COLLAB, "TC", collaborative=True, default_scope="user")
    agent_store.create_agent(TGT_SHARED, "TS", collaborative=False, default_scope="agent")
    agent_store.create_agent(TGT_PERSONAL, "TP", collaborative=False, default_scope="user")
    agent_store.create_agent(NO_EDGE, "NE", collaborative=True, default_scope="user")
    agent_store.set_delegation_targets(SRC, [TGT_COLLAB, TGT_SHARED, TGT_PERSONAL])
    mcp_store.set_mcp_enabled("delegation-mcp", True)
    monkeypatch.setattr(
        task_store, "get_username_by_sub",
        lambda sub: "alice" if sub == ALICE else None,
    )
    # Source trees with a few real files.
    user_ws = config.get_agent_dir(SRC) / "users" / "alice" / "workspace"
    (user_ws / "reports").mkdir(parents=True, exist_ok=True)
    (user_ws / "notes.md").write_text("notes")
    (user_ws / "reports" / "q3.md").write_text("q3 report")
    (user_ws / "reports" / "data.csv").write_text("a,b\n1,2")
    shared_ws = config.get_agent_dir(SRC) / "workspace"
    shared_ws.mkdir(parents=True, exist_ok=True)
    (shared_ws / "shared.txt").write_text("shared")
    return temp_db


def _authz(user, target=TGT_COLLAB, scope="user", **kw):
    return authorize_send_files(
        user, target_agent=target, requested_scope=scope, **kw,
    )


def _denied(user, status, target=TGT_COLLAB, scope="user", **kw):
    with pytest.raises(HTTPException) as exc:
        _authz(user, target=target, scope=scope, **kw)
    assert exc.value.status_code == status
    return exc.value


def _send(user, paths, target=TGT_COLLAB, scope="user", **kw):
    return perform_send_files(_authz(user, target=target, scope=scope), paths=paths, **kw)


# ───────────────────────────────────────────────────────────────────────────
# Authorization matrix
# ───────────────────────────────────────────────────────────────────────────


class TestGates:
    def test_kill_switch_missing_row(self, temp_db):
        agent_store.create_agent(SRC, "Src")
        _denied(_user_session(), 403)

    def test_kill_switch_disabled(self, send_env):
        mcp_store.set_mcp_enabled("delegation-mcp", False)
        _denied(_user_session(), 403)

    def test_cookie_caller_has_no_source_tree(self, send_env):
        _denied(_cookie(), 400)

    def test_self_target_rejected(self, send_env):
        _denied(_user_session(), 400, target=SRC)

    def test_missing_roster_edge(self, send_env):
        err = _denied(_user_session(), 403, target=NO_EDGE)
        assert "delegation targets" in err.detail

    def test_user_without_target_access(self, send_env):
        user = _user_session(agents=[SRC], agent_roles={SRC: "editor"})
        _denied(user, 403, target=TGT_COLLAB)

    def test_user_dest_lands_in_user_tree(self, send_env):
        authz = _authz(_user_session("viewer"))
        assert authz.dest_scope == "user"
        assert authz.owner_sub == ALICE
        expected = (
            config.get_agent_dir(TGT_COLLAB) / "users" / "alice"
            / "workspace" / "inbox" / SRC
        )
        assert authz.dest_root == expected

    def test_shared_only_target_clamps_with_note(self, send_env):
        authz = _authz(_user_session("editor"), target=TGT_SHARED)
        assert authz.dest_scope == "agent"
        assert authz.owner_sub == ""
        assert "not offered" in authz.scope_note
        assert authz.dest_root == (
            config.get_agent_dir(TGT_SHARED) / "workspace" / "inbox" / SRC
        )

    def test_clamped_shared_dest_gates_viewer(self, send_env):
        _denied(_user_session("viewer"), 403, target=TGT_SHARED)

    def test_contributor_drops_into_shared_workspace(self, send_env):
        # A drop is a file write: the workspace tier, not the editor tier.
        authz = _authz(_user_session("contributor"), target=TGT_SHARED)
        assert authz.dest_scope == "agent"

    def test_agent_scope_dest_needs_editor(self, send_env):
        _denied(_user_session("viewer"), 403, target=TGT_COLLAB, scope="agent")

    def test_svc_session_agent_to_agent(self, send_env):
        authz = _authz(_svc_session(), target=TGT_SHARED, scope="agent")
        assert authz.created_by == SRC
        assert authz.acting_sub is None
        assert authz.dest_scope == "agent"

    def test_svc_session_user_only_target_denied(self, send_env):
        # Personal-only target clamps agent→user, which needs a user identity.
        _denied(_svc_session(), 403, target=TGT_PERSONAL, scope="agent")

    def test_svc_session_user_scope_source_denied(self, send_env):
        _denied(_svc_session(), 400, scope="user")

    def test_daily_quota(self, send_env):
        mcp_store.set_mcp_config_values(
            "delegation-mcp", {"SEND_FILES_MAX_PER_DAY": "2"},
        )
        for _ in range(2):
            db_file_transfers.record_transfer(
                source_agent=SRC, target_agent=TGT_COLLAB, scope="user",
                owner_sub=ALICE, dest_dir="", file_count=1, total_bytes=1,
                note="", created_by=ALICE,
            )
        err = _denied(_user_session(), 403)
        assert "SEND_FILES_MAX_PER_DAY" in err.detail


# ───────────────────────────────────────────────────────────────────────────
# Path validation
# ───────────────────────────────────────────────────────────────────────────


class TestPaths:
    def test_absolute_path_rejected(self, send_env):
        with pytest.raises(HTTPException) as exc:
            _send(_user_session(), ["/etc/passwd"])
        assert exc.value.status_code == 400

    def test_dotdot_rejected(self, send_env):
        with pytest.raises(HTTPException) as exc:
            _send(_user_session(), ["../../../config/agent.md"])
        assert exc.value.status_code == 400

    def test_missing_path_404(self, send_env):
        with pytest.raises(HTTPException) as exc:
            _send(_user_session(), ["nope.md"])
        assert exc.value.status_code == 404

    def test_symlink_skipped(self, send_env):
        ws = config.get_agent_dir(SRC) / "users" / "alice" / "workspace"
        (ws / "link.md").symlink_to(ws / "notes.md")
        result = _send(_user_session(), ["link.md", "notes.md"])
        assert len(result.landed) == 1
        assert result.skipped == ["link.md (symlink)"]

    def test_symlink_escape_inside_dir_skipped(self, send_env):
        ws = config.get_agent_dir(SRC) / "users" / "alice" / "workspace"
        (ws / "reports" / "escape").symlink_to(config.get_agent_dir(SRC) / "config")
        result = _send(_user_session(), ["reports"])
        assert len(result.landed) == 2  # q3.md + data.csv only
        assert any("symlink" in s for s in result.skipped)

    def test_planted_partial_link_never_redirects_the_copy(self, send_env, tmp_path):
        # A writer of the target workspace plants `<name>.partial` as a link
        # to a file outside it: the proxy-side copy creates its own
        # exclusive temporary and never touches the planted name.
        outside = tmp_path / "outside.txt"
        outside.write_text("ORIGINAL")
        inbox = (config.get_agent_dir(TGT_COLLAB) / "users" / "alice"
                 / "workspace" / "inbox" / SRC)
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / "notes.md.partial").symlink_to(outside)
        result = _send(_user_session(), ["notes.md"])
        assert outside.read_text() == "ORIGINAL"
        assert result.landed == [f"users/alice/workspace/inbox/{SRC}/notes.md"]
        assert (inbox / "notes.md").read_text() == "notes"
        assert (inbox / "notes.md.partial").is_symlink()
        assert sorted(p.name for p in inbox.iterdir()) == ["notes.md", "notes.md.partial"]

    def test_swapped_inbox_folder_never_redirects_the_copy(self, send_env, tmp_path):
        # The same writer swaps a DIRECTORY component (its own `inbox/<src>`)
        # for a link to a folder outside the workspace. The copy walks every
        # component without following, so the link refuses the transfer and
        # nothing is created on the far side, whenever the swap happens.
        outside = tmp_path / "outside"
        outside.mkdir()
        inbox = (config.get_agent_dir(TGT_COLLAB) / "users" / "alice"
                 / "workspace" / "inbox")
        inbox.mkdir(parents=True, exist_ok=True)
        (inbox / SRC).symlink_to(outside)
        with pytest.raises(HTTPException) as exc:
            _send(_user_session(), ["notes.md"])
        assert exc.value.status_code == 400
        assert "escapes the target workspace" in exc.value.detail
        assert list(outside.iterdir()) == []
        assert (inbox / SRC).is_symlink()

    def test_a_race_on_the_name_takes_the_next_free_one(self, send_env, monkeypatch):
        # Two sends pick the same free name at once: the exclusive rename
        # refuses the second, which moves on to the next name instead of
        # overwriting the first or failing the transfer.
        from services.delegation import file_transfer
        inbox = (config.get_agent_dir(TGT_COLLAB) / "users" / "alice"
                 / "workspace" / "inbox" / SRC)
        real = file_transfer.free_name
        answers: list[str] = []

        def racy(dst_fd, parent_rel, name):
            free = real(dst_fd, parent_rel, name)
            answers.append(free)
            if len(answers) == 1:
                (inbox / free).write_text("THE OTHER SENDER WON")
            return free

        monkeypatch.setattr(file_transfer, "free_name", racy)
        result = _send(_user_session(), ["notes.md"])
        assert answers == ["notes.md", "notes_1.md"]
        assert result.landed == [f"users/alice/workspace/inbox/{SRC}/notes_1.md"]
        assert (inbox / "notes.md").read_text() == "THE OTHER SENDER WON"
        assert (inbox / "notes_1.md").read_text() == "notes"
        assert not [p for p in inbox.iterdir() if p.name.endswith(".partial")]

    def test_only_symlinks_nothing_to_send(self, send_env):
        ws = config.get_agent_dir(SRC) / "users" / "alice" / "workspace"
        (ws / "only-link.md").symlink_to(ws / "notes.md")
        with pytest.raises(HTTPException) as exc:
            _send(_user_session(), ["only-link.md"])
        assert exc.value.status_code == 400

    def test_max_files_cap(self, send_env):
        mcp_store.set_mcp_config_values(
            "delegation-mcp", {"SEND_FILES_MAX_FILES": "2"},
        )
        with pytest.raises(HTTPException) as exc:
            _send(_user_session(), ["reports", "notes.md"])
        assert exc.value.status_code == 413

    def test_per_file_size_cap(self, send_env, monkeypatch):
        monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_BYTES", 4)
        with pytest.raises(HTTPException) as exc:
            _send(_user_session(), ["notes.md"])
        assert exc.value.status_code == 413


# ───────────────────────────────────────────────────────────────────────────
# Copy mechanics + the transfer row
# ───────────────────────────────────────────────────────────────────────────


class TestCopy:
    def test_single_file_lands_by_basename(self, send_env):
        result = _send(_user_session(), ["reports/q3.md"], note="the Q3 report")
        dest = (
            config.get_agent_dir(TGT_COLLAB) / "users" / "alice"
            / "workspace" / "inbox" / SRC / "q3.md"
        )
        assert dest.read_text() == "q3 report"
        assert result.landed == [str(dest.relative_to(config.get_agent_dir(TGT_COLLAB)))]
        assert result.total_bytes == len("q3 report")

    def test_directory_preserves_structure(self, send_env):
        _send(_user_session(), ["reports"])
        base = (
            config.get_agent_dir(TGT_COLLAB) / "users" / "alice"
            / "workspace" / "inbox" / SRC / "reports"
        )
        assert (base / "q3.md").is_file()
        assert (base / "data.csv").is_file()

    def test_conflict_renames_never_overwrites(self, send_env):
        _send(_user_session(), ["notes.md"])
        _send(_user_session(), ["notes.md"])
        base = (
            config.get_agent_dir(TGT_COLLAB) / "users" / "alice"
            / "workspace" / "inbox" / SRC
        )
        assert (base / "notes.md").is_file()
        assert (base / "notes_1.md").is_file()

    def test_dest_dir_subfolder(self, send_env):
        result = _send(_user_session(), ["notes.md"], dest_dir="q3-review")
        assert result.landed[0].endswith(f"inbox/{SRC}/q3-review/notes.md")

    def test_invalid_dest_dir(self, send_env):
        with pytest.raises(HTTPException) as exc:
            _send(_user_session(), ["notes.md"], dest_dir="../up")
        assert exc.value.status_code == 400

    def test_agent_scope_source_and_dest(self, send_env):
        result = _send(_svc_session(), ["shared.txt"], target=TGT_SHARED, scope="agent")
        dest = (
            config.get_agent_dir(TGT_SHARED) / "workspace" / "inbox" / SRC
            / "shared.txt"
        )
        assert dest.read_text() == "shared"
        assert result.landed == ["workspace/inbox/%s/shared.txt" % SRC]

    def test_transfer_row_recorded(self, send_env):
        result = _send(_user_session(), ["reports"], dest_dir="drop", note="x" * 600)
        row = db_file_transfers.get_transfer(result.transfer_id)
        assert row is not None
        assert row["source_agent"] == SRC
        assert row["target_agent"] == TGT_COLLAB
        assert row["scope"] == "user"
        assert row["owner_sub"] == ALICE
        assert row["dest_dir"] == "drop"
        assert row["file_count"] == 2
        assert len(row["note"]) == 500  # note capped
        assert row["seen_at"] is None   # reserved column, never written
        assert db_file_transfers.count_recent_by_creator(ALICE) == 1


# ───────────────────────────────────────────────────────────────────────────
# 2026-09-02 security-lane regressions
# ───────────────────────────────────────────────────────────────────────────


class TestSourceClaimAccess:
    def test_cookie_caller_cannot_claim_inaccessible_source(self, send_env):
        """A dashboard-cookie user naming a source they can't access must be
        refused — the roster edge alone must not select the read root."""
        outsider = UserContext(
            sub=ALICE, email="alice@test.com", name="Alice", role="member",
            agents=[TGT_COLLAB], agent_roles={TGT_COLLAB: "editor"},
        )
        with pytest.raises(HTTPException) as e:
            authorize_send_files(
                outsider, target_agent=TGT_COLLAB, requested_scope="user",
                source_agent=SRC,
            )
        assert e.value.status_code == 403
        assert "source agent" in e.value.detail

    def test_cookie_caller_with_source_access_still_works(self, send_env):
        authz = authorize_send_files(
            _cookie(), target_agent=TGT_COLLAB, requested_scope="user",
            source_agent=SRC,
        )
        assert authz.source_agent == SRC


class TestSymlinkedDestRefused:
    def test_symlinked_dest_dir_cannot_redirect_the_copy(self, send_env, tmp_path):
        """The target agent's sandbox writes its own workspace directly — a
        symlinked inbox must not redirect the proxy-privileged copy outside
        the transfer root."""
        authz = authorize_send_files(
            _svc_session(), target_agent=TGT_SHARED, requested_scope="agent",
        )
        outside = tmp_path / "outside"
        outside.mkdir()
        tgt_ws = config.get_agent_dir(TGT_SHARED) / "workspace"
        tgt_ws.mkdir(parents=True, exist_ok=True)
        (tgt_ws / "inbox").symlink_to(outside)
        with pytest.raises(HTTPException) as e:
            perform_send_files(authz, paths=["shared.txt"], dest_dir="inbox")
        assert e.value.status_code == 400
        assert "escapes the target workspace" in e.value.detail
        assert list(outside.iterdir()) == []


# ───────────────────────────────────────────────────────────────────────────
# 2026-09-05 remote sources — read-through before the copy
# ───────────────────────────────────────────────────────────────────────────


USER_WS = "users/alice/workspace"


class _FakeInfo:
    """Stand-in for RemoteSessionInfo (what remote_file_flow consumes)."""

    def __init__(self, machine_id: str = "m-1", agent_name: str = SRC):
        self.machine_id = machine_id
        self.agent_name = agent_name


class _FakeSatellite:
    """A satellite-side agent tree served by a mocked connection manager:
    ``file_stat`` / ``file_pull`` / ``request_manifest`` over agent-dir-
    relative paths, with the real helpers' failure shapes (pull mkdirs the
    parent chain before asking, returns False when not connected)."""

    def __init__(self, tree: dict[str, bytes], *, online: bool = True,
                 stat: bool = True, name: str = "drill-sat"):
        self.tree = dict(tree)
        self.online = online
        self.stat = stat
        self.name = name
        self.probes: list[str] = []
        self.pulls: list[str] = []
        self.manifests = 0
        cm = MagicMock()
        cm.satellite_supports_file_stat.side_effect = lambda mid: self.online and self.stat
        cm.stat_file = AsyncMock(side_effect=self._stat)
        cm.pull_file_to_path = AsyncMock(side_effect=self._pull)
        cm.send_command = AsyncMock(side_effect=self._command)
        cm.get_connection.side_effect = (
            lambda mid: SimpleNamespace(name=self.name) if self.online else None
        )
        cm.get_connected_machines.return_value = []
        self.cm = cm

    async def _stat(self, machine_id, ref, agent_slug=""):
        self.probes.append(ref.value)
        data = self.tree.get(ref.value)
        if data is None:
            return {"exists": False, "size": 0, "mtime_ns": 0}
        return {"exists": True, "size": len(data), "mtime_ns": 1}

    async def _pull(self, machine_id, ref, dest_path, *, agent_slug="", timeout=180.0):
        self.pulls.append(ref.value)
        if not self.online:
            return False
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)   # the real helper does this first
        data = self.tree.get(ref.value)
        if data is None:
            return False
        dest.write_bytes(data)
        return True

    async def _command(self, machine_id, msg, *, timeout=30.0, command_id=None):
        assert msg["type"] == "request_manifest"
        self.manifests += 1
        if not self.online:
            raise RuntimeError("Satellite m-1 not connected")
        return {"files": [
            {"path": p, "size": len(b), "mtime": 1.0} for p, b in self.tree.items()
        ]}


@contextmanager
def _remote(sat: _FakeSatellite, agent: str = SRC):
    with patch.object(
        remote_file_flow, "_get_remote_session_info",
        return_value=_FakeInfo(agent_name=agent),
    ), patch(
        "core.remote.satellite_connection.get_connection_manager",
        return_value=sat.cm,
    ):
        yield


@pytest.fixture
def flow_reset():
    remote_file_flow._sessions.clear()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._pull_stat_records.clear()
    yield
    remote_file_flow._sessions.clear()
    remote_file_flow._global_path_locks.clear()
    remote_file_flow._pull_stat_records.clear()


def _inbox(target=TGT_COLLAB) -> Path:
    return config.get_agent_dir(target) / USER_WS / "inbox" / SRC


class TestValidateSendPath:
    def test_normalizes_to_the_manifest_form(self):
        assert validate_send_path("./reports//q3.md/") == "reports/q3.md"
        assert validate_send_path("  notes.md ") == "notes.md"

    @pytest.mark.parametrize("bad", ["", "   ", "/etc/passwd", "\\x", "a/../b", "../x"])
    def test_rejects_what_perform_rejects(self, bad):
        with pytest.raises(HTTPException) as exc:
            validate_send_path(bad)
        assert exc.value.status_code == 400


class TestRemotePrefetch:
    @pytest.mark.asyncio
    async def test_local_session_never_touches_the_satellite(self, send_env, flow_reset):
        sat = _FakeSatellite({f"{USER_WS}/notes.md": b"v2"})
        authz = _authz(_user_session())
        with patch.object(
            remote_file_flow, "_get_remote_session_info", return_value=None,
        ), patch(
            "core.remote.satellite_connection.get_connection_manager",
            return_value=sat.cm,
        ):
            assert await prefetch_remote_sources("s-1", authz, ["notes.md"]) == []
        assert (sat.probes, sat.pulls, sat.manifests) == ([], [], 0)
        assert (config.get_agent_dir(SRC) / USER_WS / "notes.md").read_text() == "notes"

    @pytest.mark.asyncio
    async def test_same_turn_write_is_pulled_before_the_copy(self, send_env, flow_reset):
        """The loud variant: absent platform-side, written on the satellite
        this turn — pulled into the user-scope prefix, then copied."""
        sat = _FakeSatellite({f"{USER_WS}/today.md": b"written this turn"})
        authz = _authz(_user_session())
        with _remote(sat):
            unavailable = await prefetch_remote_sources(
                "s-1", authz, ["today.md"], max_files=20,
            )
        assert unavailable == []
        # The prefetch's own probe, then pull_through's revalidation probe.
        assert sat.probes == [f"{USER_WS}/today.md"] * 2
        assert sat.pulls == [f"{USER_WS}/today.md"]
        result = perform_send_files(authz, paths=["today.md"])
        assert (_inbox() / "today.md").read_bytes() == b"written this turn"
        assert result.landed == [f"{USER_WS}/inbox/{SRC}/today.md"]

    @pytest.mark.asyncio
    async def test_modified_this_turn_sends_the_current_bytes(self, send_env, flow_reset):
        """The silent variant: a platform copy exists (turn-end synced,
        no pull-stat record) — the pull is unconditional, so the newer
        satellite bytes ship, not the stale copy."""
        sat = _FakeSatellite({f"{USER_WS}/notes.md": b"notes v2"})
        authz = _authz(_user_session())
        with _remote(sat):
            await prefetch_remote_sources("s-1", authz, ["notes.md"], max_files=20)
        assert sat.pulls == [f"{USER_WS}/notes.md"]
        perform_send_files(authz, paths=["notes.md"])
        assert (_inbox() / "notes.md").read_bytes() == b"notes v2"

    @pytest.mark.asyncio
    async def test_unchanged_since_last_pull_is_served_from_the_copy(self, send_env, flow_reset):
        """Stat fast path: a second send of an unchanged file re-probes but
        does not re-transfer."""
        sat = _FakeSatellite({f"{USER_WS}/notes.md": b"notes v2"})
        authz = _authz(_user_session())
        with _remote(sat):
            await prefetch_remote_sources("s-1", authz, ["notes.md"], max_files=20)
            await prefetch_remote_sources("s-1", authz, ["notes.md"], max_files=20)
        assert sat.pulls == [f"{USER_WS}/notes.md"]
        assert len(sat.probes) == 4   # prefetch probe + pull_through's own, twice

    @pytest.mark.asyncio
    async def test_agent_scope_uses_the_shared_prefix(self, send_env, flow_reset):
        sat = _FakeSatellite({"workspace/shared.txt": b"shared v2"})
        authz = _authz(_svc_session(), target=TGT_SHARED, scope="agent")
        with _remote(sat):
            await prefetch_remote_sources("s-svc", authz, ["shared.txt"], max_files=20)
        assert sat.pulls == ["workspace/shared.txt"]
        result = perform_send_files(authz, paths=["shared.txt"])
        dest = config.get_agent_dir(TGT_SHARED) / "workspace" / "inbox" / SRC / "shared.txt"
        assert dest.read_bytes() == b"shared v2"
        assert result.landed == [f"workspace/inbox/{SRC}/shared.txt"]

    @pytest.mark.asyncio
    async def test_directory_is_one_manifest_then_per_file_pulls(self, send_env, flow_reset):
        sat = _FakeSatellite({
            f"{USER_WS}/reports/q3.md": b"q3 v2",
            f"{USER_WS}/reports/new.csv": b"x,y",
            f"{USER_WS}/reports/tmp.csv.partial": b"torn",
            f"{USER_WS}/notes.md": b"unrelated",
        })
        authz = _authz(_user_session())
        with _remote(sat):
            unavailable = await prefetch_remote_sources(
                "s-1", authz, ["reports"], max_files=20,
            )
        assert unavailable == []
        assert sat.manifests == 1
        # A platform-side directory is never probed (or pulled) as a file;
        # the only probes are pull_through's own, per pulled entry.
        assert f"{USER_WS}/reports" not in sat.probes
        assert sat.pulls == [f"{USER_WS}/reports/new.csv", f"{USER_WS}/reports/q3.md"]
        perform_send_files(authz, paths=["reports"])
        base = _inbox() / "reports"
        assert (base / "q3.md").read_bytes() == b"q3 v2"
        assert (base / "new.csv").read_bytes() == b"x,y"
        # Deleted-on-satellite-this-turn lag: the platform copy still ships.
        assert (base / "data.csv").is_file()
        assert not (base / "tmp.csv.partial").exists()

    @pytest.mark.asyncio
    async def test_directory_new_on_the_satellite(self, send_env, flow_reset):
        sat = _FakeSatellite({
            f"{USER_WS}/out/a.txt": b"a",
            f"{USER_WS}/out/sub/b.txt": b"b",
        })
        authz = _authz(_user_session())
        with _remote(sat):
            unavailable = await prefetch_remote_sources("s-1", authz, ["out"], max_files=20)
        assert unavailable == []
        # Not a platform dir → probed; exists=False (a dir) → no pull of the
        # dir itself (no failed pull, no junk), then the manifest; the two
        # remaining probes are pull_through's, per pulled entry.
        assert sat.probes == [
            f"{USER_WS}/out", f"{USER_WS}/out/a.txt", f"{USER_WS}/out/sub/b.txt",
        ]
        assert sat.pulls == [f"{USER_WS}/out/a.txt", f"{USER_WS}/out/sub/b.txt"]
        result = perform_send_files(authz, paths=["out"])
        assert sorted(Path(p).name for p in result.landed) == ["a.txt", "b.txt"]
        assert (_inbox() / "out" / "sub" / "b.txt").read_bytes() == b"b"

    @pytest.mark.asyncio
    async def test_directory_pulls_stop_at_the_call_cap(self, send_env, flow_reset):
        sat = _FakeSatellite({f"{USER_WS}/big/f{i}.txt": b"x" for i in range(6)})
        authz = _authz(_user_session())
        with _remote(sat):
            await prefetch_remote_sources("s-1", authz, ["big"], max_files=2)
        assert len(sat.pulls) == 3   # max_files + 1 — perform's 413 decides
        mcp_store.set_mcp_config_values(
            "delegation-mcp", {"SEND_FILES_MAX_FILES": "2"},
        )
        with pytest.raises(HTTPException) as exc:
            perform_send_files(authz, paths=["big"])
        assert exc.value.status_code == 413

    @pytest.mark.asyncio
    async def test_cap_is_read_from_config_when_not_given(self, send_env, flow_reset):
        mcp_store.set_mcp_config_values(
            "delegation-mcp", {"SEND_FILES_MAX_FILES": "1"},
        )
        sat = _FakeSatellite({f"{USER_WS}/big/f{i}.txt": b"x" for i in range(4)})
        authz = _authz(_user_session())
        with _remote(sat):
            await prefetch_remote_sources("s-1", authz, ["big"])
        assert len(sat.pulls) == 2

    @pytest.mark.asyncio
    async def test_typo_never_mkdirs_and_is_reported_unavailable(self, send_env, flow_reset):
        sat = _FakeSatellite({f"{USER_WS}/notes.md": b"notes"})
        authz = _authz(_user_session())
        with _remote(sat):
            unavailable = await prefetch_remote_sources(
                "s-1", authz, ["reprots/q3.md"], max_files=20,
            )
        assert unavailable == ["reprots/q3.md"]
        assert sat.pulls == []        # exists=False → no pull → no parent-chain mkdir
        assert sat.manifests == 1
        assert not (config.get_agent_dir(SRC) / USER_WS / "reprots").exists()
        with pytest.raises(MissingSourcePath) as exc:
            perform_send_files(authz, paths=["reprots/q3.md"])
        assert exc.value.status_code == 404
        assert exc.value.raw == "reprots/q3.md"
        assert exc.value.detail == "No such file or directory in your workspace: 'reprots/q3.md'"

    @pytest.mark.asyncio
    async def test_old_satellite_without_stat_still_pulls(self, send_env, flow_reset):
        sat = _FakeSatellite({f"{USER_WS}/today.md": b"fresh"}, stat=False)
        authz = _authz(_user_session())
        with _remote(sat):
            unavailable = await prefetch_remote_sources(
                "s-1", authz, ["today.md"], max_files=20,
            )
        assert unavailable == [] and sat.probes == []
        assert sat.pulls == [f"{USER_WS}/today.md"]
        assert (config.get_agent_dir(SRC) / USER_WS / "today.md").read_bytes() == b"fresh"

    @pytest.mark.asyncio
    async def test_offline_satellite_degrades_to_the_platform_tree(self, send_env, flow_reset):
        sat = _FakeSatellite({f"{USER_WS}/reports/q3.md": b"never served"}, online=False)
        authz = _authz(_user_session())
        with _remote(sat):
            unavailable = await prefetch_remote_sources(
                "s-1", authz, ["notes.md", "reports", "gone.md"], max_files=20,
            )
        # notes.md: pull fails → platform mirror; reports: manifest fails →
        # platform copy; gone.md: nothing on either side.
        assert unavailable == ["reports", "gone.md"]
        assert sat.manifests == 1   # one attempt per call, not per path
        result = perform_send_files(authz, paths=["notes.md", "reports"])
        assert len(result.landed) == 3
        assert (_inbox() / "notes.md").read_text() == "notes"
        assert (_inbox() / "reports" / "q3.md").read_text() == "q3 report"
        with pytest.raises(MissingSourcePath):
            perform_send_files(authz, paths=["gone.md"])

    @pytest.mark.asyncio
    async def test_invalid_paths_never_reach_the_satellite(self, send_env, flow_reset):
        sat = _FakeSatellite({})
        authz = _authz(_user_session())
        bad = ["../../config/agent.md", "/etc/passwd", ""]
        with _remote(sat):
            unavailable = await prefetch_remote_sources("s-1", authz, bad, max_files=20)
        assert unavailable == []
        assert (sat.probes, sat.pulls, sat.manifests) == ([], [], 0)
        for raw in bad:
            with pytest.raises(HTTPException) as exc:
                perform_send_files(authz, paths=[raw])
            assert exc.value.status_code == 400

    @pytest.mark.asyncio
    async def test_a_failed_read_sends_the_platform_copy_and_says_so(
        self, send_env, flow_reset, caplog,
    ):
        sat = _FakeSatellite({f"{USER_WS}/notes.md": b"newer on the machine"})
        sat.cm.pull_file_to_path = AsyncMock(return_value=False)
        authz = _authz(_user_session())
        with _remote(sat), caplog.at_level("INFO", logger="claude-proxy"):
            assert await prefetch_remote_sources("s-1", authz, ["notes.md"], max_files=20) == []
        assert (f"{USER_WS}/notes.md could not be read from 'drill-sat', the platform's "
                "copy was sent") in caplog.text
        assert "read through=0 fallback=['" in caplog.text

    @pytest.mark.asyncio
    async def test_a_read_past_the_budget_is_abandoned_for_the_platform_copy(
        self, send_env, flow_reset, monkeypatch, caplog,
    ):
        """The delegation MCP stops waiting at 300 s: a pull still running at
        the budget is cancelled and the platform's copy goes, reported."""
        from services.delegation import file_transfer
        monkeypatch.setattr(file_transfer, "_PREFETCH_BUDGET_S", 0.2)
        sat = _FakeSatellite({f"{USER_WS}/notes.md": b"a slow link"})
        cancelled = asyncio.Event()

        async def _endless(*_a, **_k):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        sat.cm.pull_file_to_path = AsyncMock(side_effect=_endless)
        authz = _authz(_user_session())
        with _remote(sat), caplog.at_level("WARNING", logger="claude-proxy"):
            got = await asyncio.wait_for(
                prefetch_remote_sources("s-1", authz, ["notes.md"], max_files=20), 5)
        assert got == []
        assert cancelled.is_set()
        assert "the platform's copy was sent" in caplog.text
        assert (config.get_agent_dir(SRC) / USER_WS / "notes.md").read_text() == "notes"

    @pytest.mark.asyncio
    async def test_prefetch_defects_degrade_and_never_abort(self, send_env, flow_reset):
        authz = _authz(_user_session())
        with patch.object(
            remote_file_flow, "_get_remote_session_info", return_value=_FakeInfo(),
        ), patch.object(
            remote_file_flow, "stat_probe", AsyncMock(side_effect=RuntimeError("probe")),
        ), patch.object(
            remote_file_flow, "pull_through", AsyncMock(side_effect=RuntimeError("pull")),
        ), patch.object(
            remote_file_flow, "list_remote_files", AsyncMock(side_effect=RuntimeError("mf")),
        ):
            unavailable = await prefetch_remote_sources(
                "s-1", authz, ["notes.md", "reports"], max_files=20,
            )
        assert unavailable == ["notes.md", "reports"]
        result = perform_send_files(authz, paths=["notes.md", "reports"])
        assert len(result.landed) == 3


def _endpoint_client(user: UserContext) -> TestClient:
    from api.tasks import delegation as delegation_api
    from auth.providers import get_current_user

    app = FastAPI()
    app.include_router(delegation_api.router)

    async def _current():
        return user

    app.dependency_overrides[get_current_user] = _current
    return TestClient(app)


def _post(client: TestClient, paths: list[str], *, target=TGT_COLLAB, scope="user"):
    return client.post("/v1/delegation/send_files", json={
        "target_agent": target, "paths": paths, "scope": scope,
    })


class TestEndpointRemoteSources:
    def test_same_turn_write_lands_through_the_endpoint(self, send_env, flow_reset):
        sat = _FakeSatellite({f"{USER_WS}/today.md": b"fresh"})
        client = _endpoint_client(_user_session())
        with _remote(sat):
            r = _post(client, ["today.md"])
        assert r.status_code == 200, r.text
        assert r.json()["files"] == [f"{USER_WS}/inbox/{SRC}/today.md"]
        assert (_inbox() / "today.md").read_bytes() == b"fresh"

    def test_remote_404_names_the_connected_machine(self, send_env, flow_reset):
        sat = _FakeSatellite({}, name="drill-sat")
        client = _endpoint_client(_user_session())
        with _remote(sat):
            r = _post(client, ["missing.md"])
        assert r.status_code == 404
        assert r.json()["detail"] == (
            "'missing.md' is not in the platform copy of your workspace and "
            "the remote machine 'drill-sat' could not provide it (offline, or "
            "the file does not exist there)"
        )

    def test_remote_404_offline_name_comes_from_the_store(self, send_env, flow_reset, monkeypatch):
        sat = _FakeSatellite({}, online=False)
        monkeypatch.setattr(
            remote_store, "get_remote_machine",
            lambda mid: {"id": mid, "name": "my laptop"} if mid == "m-1" else None,
        )
        client = _endpoint_client(_user_session())
        with _remote(sat):
            r = _post(client, ["missing.md"])
        assert r.status_code == 404
        assert "the remote machine 'my laptop' could not provide it" in r.json()["detail"]

    def test_remote_404_only_for_paths_the_satellite_lacked(self, send_env, flow_reset):
        """A path that is missing platform-side but exists on the satellite
        is pulled, so it never 404s; one that neither side has gets the
        remote wording — and a local session keeps the plain detail."""
        sat = _FakeSatellite({f"{USER_WS}/today.md": b"fresh"})
        client = _endpoint_client(_user_session())
        with _remote(sat):
            r = _post(client, ["today.md", "missing.md"])
        assert r.status_code == 404
        assert r.json()["detail"].startswith("'missing.md' is not in the platform copy")
        with patch.object(remote_file_flow, "_get_remote_session_info", return_value=None):
            r = _post(client, ["missing.md"])
        assert r.status_code == 404
        assert r.json()["detail"] == "No such file or directory in your workspace: 'missing.md'"

    def test_cookie_caller_has_no_session_and_no_prefetch(self, send_env, flow_reset):
        """A dashboard caller (no session id) never enters the prefetch — the
        400 from authorize fires exactly as before."""
        sat = _FakeSatellite({})
        client = _endpoint_client(_cookie())
        with _remote(sat):
            r = _post(client, ["notes.md"])
        assert r.status_code == 400
        assert (sat.probes, sat.pulls, sat.manifests) == ([], [], 0)
