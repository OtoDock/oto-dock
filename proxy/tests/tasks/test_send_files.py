"""Cross-agent file transfer (delegation-mcp ``send_files``).

``services/delegation/file_transfer`` is the single gate + copy engine the
``/v1/delegation/send_files`` endpoint calls. Covers: the spawn-mirroring
authz matrix (kill-switch, source identity, roster edge, target access,
scope clamp, editor tier, daily quota), path validation (traversal,
symlink escape, recursion), copy mechanics (basename landing, structure
preservation, conflict renames, caps), and the audit row. There is
deliberately NO session-start notify (removed 2026-08-14 — the prompt's
workspace listing is the discovery surface).

Run: cd proxy && python -m pytest tests/tasks/test_send_files.py -v
"""

import shutil

import pytest
from fastapi import HTTPException

import config
from auth.providers import UserContext
from services.delegation.file_transfer import (
    authorize_send_files,
    perform_send_files,
)
from storage import agent_store, db_file_transfers, mcp_store
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
