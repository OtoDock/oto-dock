"""Satellite per-user isolation security tests.

Three classes of test:
  1. `compute_manifest` blacklist filter when ``target_username`` is set
     (and unfiltered when None).
  2. `diff_manifests` marks blacklisted satellite-side paths for delete
     so legacy leaks scrub on next sync.
  3. Admin-only gate on agent default execution target: `assign_agent`
     and `update_agent` both reject non-admin-owned target machines.
  4. `session_manager.get_execution_layer` refuses agent-scope sessions
     (``user_sub=None``) routed to a user-paired machine.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.remote.file_sync import compute_manifest, diff_manifests, FileEntry
from tests._paths import PROXY_DIR


# ---------------------------------------------------------------------------
# 1. compute_manifest filter
# ---------------------------------------------------------------------------


@pytest.fixture
def multi_user_agent_dir(tmp_path):
    """Agent dir with two users + agent-scope sensitive dirs + neutral data."""
    # alice's stuff
    (tmp_path / "users" / "alice").mkdir(parents=True)
    (tmp_path / "users" / "alice" / "notes.txt").write_text("alice notes")
    (tmp_path / "users" / "alice" / "google-tokens").mkdir()
    (tmp_path / "users" / "alice" / "google-tokens" / "a.json").write_text("{}")

    # bob's stuff (should be filtered when target = alice)
    (tmp_path / "users" / "bob").mkdir(parents=True)
    (tmp_path / "users" / "bob" / "notes.txt").write_text("bob notes")
    (tmp_path / "users" / "bob" / "google-tokens").mkdir()
    (tmp_path / "users" / "bob" / "google-tokens" / "b.json").write_text("{}")

    # agent-scope sensitive: workspace/credentials/ + service-accounts/
    (tmp_path / "workspace" / "credentials").mkdir(parents=True)
    (tmp_path / "workspace" / "credentials" / "agent-key.json").write_text("{}")
    (tmp_path / "service-accounts").mkdir()
    (tmp_path / "service-accounts" / "google.json").write_text("{}")

    # alice's hypothetical future oauth cache (matches .cache/oauth segment rule)
    (tmp_path / "users" / "alice" / ".cache").mkdir()
    (tmp_path / "users" / "alice" / ".cache" / "oauth").mkdir()
    (tmp_path / "users" / "alice" / ".cache" / "oauth" / "tok.bin").write_text("x")

    # neutral data (kept for everyone)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "prompt.md").write_text("# prompt")
    (tmp_path / "workspace" / "data.txt").write_text("workspace data")

    return tmp_path


class TestComputeManifestBlacklist:
    def test_blacklists_other_users_when_target_set(self, multi_user_agent_dir):
        # Owner-tier session (target_role="manager") on a user-paired machine:
        # user-isolation filters other users' data; config/ stays synced.
        entries = compute_manifest(
            multi_user_agent_dir, target_username="alice", target_role="manager",
        )
        paths = {e.path for e in entries}

        # alice's own non-sensitive data kept
        assert "users/alice/notes.txt" in paths
        assert "users/alice/google-tokens/a.json" in paths

        # bob's data filtered out
        assert "users/bob/notes.txt" not in paths
        assert "users/bob/google-tokens/b.json" not in paths

        # agent-scope service-account credentials filtered out
        assert not any(p.startswith("service-accounts/") for p in paths)
        # (role-v2): legacy `workspace/credentials/` and
        # `.cache/oauth/` blacklist entries removed — those paths were
        # no longer used (tokens moved to
        # `users/{u}/.credentials/{provider}-tokens/`). .cache is still
        # skipped by SKIP_DIRS on os.walk so .cache/oauth never enters
        # the manifest in any case.
        assert not any(".cache/oauth" in p for p in paths)

        # neutral data still synced (owner-tier — config/ included)
        assert "config/prompt.md" in paths
        assert "workspace/data.txt" in paths

    def test_admin_target_sees_everything(self, multi_user_agent_dir):
        # Admin-shared target → no user-isolation filter + admin role → config synced
        entries = compute_manifest(
            multi_user_agent_dir, target_username=None, target_role="admin",
        )
        paths = {e.path for e in entries}

        # everyone visible
        assert "users/alice/notes.txt" in paths
        assert "users/bob/notes.txt" in paths
        assert "users/alice/google-tokens/a.json" in paths
        assert "users/bob/google-tokens/b.json" in paths
        # agent-scope sensitive: not filtered (admin shared = full trust)
        assert "workspace/credentials/agent-key.json" in paths
        assert "service-accounts/google.json" in paths
        assert "config/prompt.md" in paths

    def test_blacklist_preserves_target_user_subpaths(self, multi_user_agent_dir):
        # Deep nesting under alice/ should all sync
        (multi_user_agent_dir / "users" / "alice" / "deep" / "nested").mkdir(parents=True)
        (multi_user_agent_dir / "users" / "alice" / "deep" / "nested" / "x.txt").write_text("x")

        entries = compute_manifest(multi_user_agent_dir, target_username="alice")
        paths = {e.path for e in entries}
        assert "users/alice/deep/nested/x.txt" in paths

    def test_orphaned_owner_filters_all_users(self, multi_user_agent_dir):
        """Empty target_username (deleted owner) → blacklist EVERY users/* path.

        Fail-safe behavior: when the machine's registered_by user has been
        deleted, the resolver in remote_execution.py coerces target_username
        to "" so no user's data leaks via the absent-owner branch. The
        running session is owner-tier (manager/admin) — that's why config/
        still syncs even when the registered_by lookup returns nothing.
        """
        entries = compute_manifest(
            multi_user_agent_dir, target_username="", target_role="manager",
        )
        paths = {e.path for e in entries}
        # No users/* paths sync
        assert not any(p.startswith("users/") for p in paths)
        # service-accounts still blacklisted (agent-scope creds)
        assert not any(p.startswith("service-accounts/") for p in paths)
        # Neutral data still flows
        assert "config/prompt.md" in paths
        assert "workspace/data.txt" in paths


# ---------------------------------------------------------------------------
# 2. diff_manifests marks blacklisted remote paths for delete
# ---------------------------------------------------------------------------


class TestDiffManifestsBlacklist:
    def test_blacklisted_remote_paths_scrubbed(self):
        local = [FileEntry("users/alice/notes.txt", "sha256:a", 10, 1.0)]
        remote = [
            {"path": "users/alice/notes.txt", "hash": "sha256:a"},     # in sync
            {"path": "users/bob/leaked.json", "hash": "sha256:b"},     # legacy leak
            {"path": "service-accounts/google.json", "hash": "sha256:c"},  # legacy leak
        ]
        plan = diff_manifests(local, remote, target_username="alice")
        # Other-user data + service-account creds that leaked onto a user-paired
        # satellite are ISOLATION-SCRUBBED (deleted there), never pulled back.
        assert "users/bob/leaked.json" in plan.to_scrub
        assert "service-accounts/google.json" in plan.to_scrub
        assert "users/alice/notes.txt" not in plan.to_scrub
        pulled = [a.rel_path for a in plan.actions if a.op == "pull"]
        assert "users/bob/leaked.json" not in pulled
        assert "service-accounts/google.json" not in pulled

    def test_admin_target_no_isolation_scrub(self):
        # Admin target (target_username=None): the per-user blacklist does NOT
        # apply — bob's satellite-only file is NOT isolation-scrubbed. It flows
        # through the ordinary 3-way merge instead. The point of this test is the
        # ABSENCE of a cross-user scrub-delete on an admin-paired machine.
        local = [FileEntry("users/alice/notes.txt", "sha256:a", 10, 1.0)]
        remote = [
            {"path": "users/bob/notes.txt", "hash": "sha256:b"},
        ]
        plan = diff_manifests(local, remote, target_username=None, target_role="admin")
        assert plan.to_scrub == []
        scrub_deleted = [a.rel_path for a in plan.actions if a.op == "delete_satellite"]
        assert "users/bob/notes.txt" not in scrub_deleted


# ---------------------------------------------------------------------------
# 3. Admin-only gate on assign_agent + update_agent
# ---------------------------------------------------------------------------


def _make_user(sub="admin-sub", role="admin"):
    """Build a UserContext for tests."""
    from auth.providers import UserContext
    return UserContext(sub=sub, email="x@y", name="x", role=role)


class TestAssignAgentAdminGate:
    @pytest.mark.asyncio
    async def test_rejects_non_admin_machine(self):
        from api.remote.remote_machines import assign_agent, AssignAgentRequest
        from fastapi import HTTPException

        admin_user = _make_user("admin-1", "admin")
        with patch("api.remote.remote_machines.remote_store.get_remote_machine") as gm:
            gm.return_value = {
                "id": "user-machine",
                "name": "alices-laptop",
                "registered_by": "alice-sub",
                "owner_role": "creator",
                "pairing_scope": "user",
            }
            with pytest.raises(HTTPException) as exc:
                await assign_agent(
                    "user-machine",
                    AssignAgentRequest(agent_slug="any"),
                    admin_user,
                )
            assert exc.value.status_code == 403
            assert "admin-paired" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_accepts_admin_machine(self):
        from api.remote.remote_machines import assign_agent, AssignAgentRequest

        admin_user = _make_user("admin-1", "admin")
        with patch("api.remote.remote_machines.remote_store") as rs, \
             patch("api.remote.remote_machines.agent_store") as ag:
            rs.get_remote_machine.return_value = {
                "id": "admin-machine",
                "name": "shared",
                "registered_by": "admin-2",
                "owner_role": "admin",
                "pairing_scope": "admin",
            }
            ag.agent_exists.return_value = True
            ag.get_agent.return_value = {"execution_path": "claude-code-cli"}
            result = await assign_agent(
                "admin-machine",
                AssignAgentRequest(agent_slug="any"),
                admin_user,
            )
            assert result == {"ok": True}
            rs.set_agent_remote_target.assert_called_once()


def _satellite_source_available() -> bool:
    from ws.satellite import satellite_source_available
    return satellite_source_available()


@pytest.mark.skipif(
    not _satellite_source_available(),
    reason="pairing needs the satellite source tree (not in this build)",
)
class TestAdminPairAllowFullFsDefault:
    """Admin pairing defaults to home-only (opt-in full-FS)."""

    @pytest.mark.asyncio
    async def test_admin_pair_defaults_home_only(self):
        from api.remote.remote_machines import pair_machine, PairMachineRequest

        admin_user = _make_user("admin-1", "admin")
        with patch("api.remote.remote_machines.remote_store") as rs, \
             patch("config.get_platform_public_url", return_value="https://oto.example"):
            rs.create_remote_machine.return_value = {
                "id": "m1", "name": "ops-box", "pairing_token": "tok",
            }
            rs.PAIRING_TOKEN_EXPIRY_HOURS = 24
            await pair_machine(PairMachineRequest(name="ops-box"), admin_user)
            _, kwargs = rs.create_remote_machine.call_args
            assert kwargs["allow_full_fs"] is False  # least privilege by default
            assert kwargs["pairing_scope"] == "admin"

    @pytest.mark.asyncio
    async def test_admin_pair_explicit_full_fs_opt_in(self):
        from api.remote.remote_machines import pair_machine, PairMachineRequest

        admin_user = _make_user("admin-1", "admin")
        with patch("api.remote.remote_machines.remote_store") as rs, \
             patch("config.get_platform_public_url", return_value="https://oto.example"):
            rs.create_remote_machine.return_value = {
                "id": "m1", "name": "ops", "pairing_token": "t",
            }
            rs.PAIRING_TOKEN_EXPIRY_HOURS = 24
            await pair_machine(
                PairMachineRequest(name="ops", allow_full_fs=True), admin_user,
            )
            _, kwargs = rs.create_remote_machine.call_args
            assert kwargs["allow_full_fs"] is True  # explicit opt-in still honored


class TestUpdateAgentAdminGate:
    @pytest.mark.asyncio
    async def test_creator_cannot_set_remote_target(self):
        """Even a platform creator can't point an agent default at any remote machine."""
        from api.agents.agents import update_agent
        from fastapi import HTTPException
        from pydantic import BaseModel

        # UpdateAgentRequest may have many fields; mock minimal
        creator_user = _make_user("mgr-1", "creator")
        # The creator has access to the agent via agent_roles (per-agent manager)
        creator_user.agents = ["my-agent"]
        creator_user.agent_roles = {"my-agent": "manager"}

        class _Req(BaseModel):
            execution_target: str | None = None
            execution_paths: list[str] | None = None
            admin_only: bool = False
            def model_dump(self, **kw):
                return {k: v for k, v in self.__dict__.items() if v is not None}

        req = _Req(execution_target="some-machine")

        with patch("api.agents.agents.agent_store") as ag:
            ag.agent_exists.return_value = True
            ag.get_agent.return_value = {"execution_path": "claude-code-cli"}
            with pytest.raises(HTTPException) as exc:
                await update_agent("my-agent", req, creator_user)
            assert exc.value.status_code == 403
            assert "only admins" in exc.value.detail.lower()

    @pytest.mark.asyncio
    async def test_admin_rejected_on_non_admin_machine(self):
        from api.agents.agents import update_agent
        from fastapi import HTTPException
        from pydantic import BaseModel

        admin_user = _make_user("admin-1", "admin")

        class _Req(BaseModel):
            execution_target: str | None = None
            execution_paths: list[str] | None = None
            admin_only: bool = False
            def model_dump(self, **kw):
                return {k: v for k, v in self.__dict__.items() if v is not None}

        req = _Req(execution_target="user-machine")

        with patch("api.agents.agents.agent_store") as ag, \
             patch("storage.remote_store.get_remote_machine") as gm:
            ag.agent_exists.return_value = True
            ag.get_agent.return_value = {"execution_path": "claude-code-cli"}
            gm.return_value = {
                "id": "user-machine",
                "name": "alices-laptop",
                "registered_by": "alice-sub",
                "owner_role": "creator",
                "pairing_scope": "user",
            }
            with pytest.raises(HTTPException) as exc:
                await update_agent("my-agent", req, admin_user)
            assert exc.value.status_code == 403
            assert "admin-paired" in exc.value.detail.lower()


# ---------------------------------------------------------------------------
# 4. session_manager refuses agent-scope on user-paired machines
# ---------------------------------------------------------------------------


class TestAgentScopeOnUserPairedRefused:
    def test_agent_scope_refused_on_user_paired(self):
        """user_sub=None routed to a user-paired machine → RuntimeError."""
        from core.session import session_manager

        with patch("storage.agent_store.get_agent") as ga, \
             patch("storage.remote_store.resolve_execution_target") as ret, \
             patch("storage.remote_store.get_remote_machine") as gm:
            ga.return_value = {"execution_path": "claude-code-cli"}
            ret.return_value = ("user-machine", None)
            gm.return_value = {
                "id": "user-machine",
                "registered_by": "alice-sub",
                "owner_role": "creator",
                "pairing_scope": "user",
            }
            with pytest.raises(RuntimeError) as exc:
                session_manager.get_execution_layer(
                    agent_name="any-agent",
                    user_sub=None,
                    role="manager",
                )
            assert "agent-scope" in str(exc.value).lower()
            assert "user-paired" in str(exc.value).lower()

    def test_user_scope_allowed_on_user_paired(self):
        """user_sub set → request proceeds even on user-paired machine."""
        from core.session import session_manager

        with patch("storage.agent_store.get_agent") as ga, \
             patch("storage.remote_store.resolve_execution_target") as ret, \
             patch("storage.remote_store.get_remote_machine") as gm, \
             patch.object(session_manager, "_get_remote_layer") as grl:
            ga.return_value = {"execution_path": "claude-code-cli"}
            ret.return_value = ("user-machine", None)
            gm.return_value = {
                "id": "user-machine",
                "registered_by": "alice-sub",
                "owner_role": "creator",
                "pairing_scope": "user",
            }
            grl.return_value = MagicMock()
            # Should NOT raise
            layer = session_manager.get_execution_layer(
                agent_name="any-agent",
                user_sub="alice-sub",
                role="manager",
            )
            assert layer is grl.return_value

    def test_agent_scope_allowed_on_admin_machine(self):
        """user_sub=None routed to an admin-shared machine → allowed."""
        from core.session import session_manager

        with patch("storage.agent_store.get_agent") as ga, \
             patch("storage.remote_store.resolve_execution_target") as ret, \
             patch("storage.remote_store.get_remote_machine") as gm, \
             patch.object(session_manager, "_get_remote_layer") as grl:
            ga.return_value = {"execution_path": "claude-code-cli"}
            ret.return_value = ("admin-machine", None)
            gm.return_value = {
                "id": "admin-machine",
                "registered_by": "admin-sub",
                "owner_role": "admin",
                "pairing_scope": "admin",
            }
            grl.return_value = MagicMock()
            layer = session_manager.get_execution_layer(
                agent_name="any-agent",
                user_sub=None,
                role="manager",
            )
            assert layer is grl.return_value


# ---------------------------------------------------------------------------
# 5. Regression guard: routing/isolation keys on pairing_scope, not owner_role
# ---------------------------------------------------------------------------


def test_routing_modules_do_not_key_on_owner_role():
    """Per-machine routing + per-user isolation MUST key on the stable
    `pairing_scope` column (set once at pairing), NEVER the mutable
    `owner_role` (derived from the owner's CURRENT platform role — promoting a
    user to admin would otherwise turn their personal user-paired laptop into
    an admin-shared machine that receives every user's folders). The owner_role
    field exists only for display/diagnostics in remote_store's queries; no
    routing module may reference it.
    """
    proxy_root = PROXY_DIR
    routing_files = [
        "core/session/session_manager.py",
        "core/remote/remote_execution.py",
        "ws/satellite.py",
        "api/agents/agents.py",
        "api/auth/auth.py",
    ]
    offenders = [
        rel for rel in routing_files
        if "owner_role" in (proxy_root / rel).read_text()
    ]
    assert not offenders, (
        f"owner_role referenced in routing module(s) {offenders}; use the "
        f"stable pairing_scope for admin-vs-user-paired decisions"
    )
