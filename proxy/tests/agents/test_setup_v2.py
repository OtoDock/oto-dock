"""Setup v2 — per-user onboarding (`user-setup.md`) + scope-aware complete-setup.

Covers ``community_agent_installer.seed_user_setup_file`` (attach-time copy
with tombstone hygiene), the ``on_user_added_to_agent`` seeding order (runs
BEFORE the community-template guard, opt-out for the reseed recovery
endpoint), and ``POST /v1/agents/{name}/complete-setup`` scope resolution +
authorization + sync bookkeeping.

Run: cd proxy && python -m pytest tests/agents/test_setup_v2.py -v
"""

from __future__ import annotations

import asyncio
import sys
import time
from unittest.mock import AsyncMock, patch

import pytest

from tests._paths import PROXY_DIR
_proxy_root = str(PROXY_DIR)
if _proxy_root not in sys.path:
    sys.path.insert(0, _proxy_root)

import config as app_config
from storage import agent_store, file_tombstones_store
from storage.pg import get_conn

AGENT = "setup-v2-agent"


def _set_username(sub: str, username: str) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE users SET username=%s WHERE sub=%s", (username, sub),
        )
        conn.commit()


def _mk_agent(slug: str = AGENT):
    if not agent_store.agent_exists(slug):
        agent_store.create_agent(slug, "Setup V2 Agent")
    return app_config.AGENTS_DIR / slug


def _viewer():
    from auth.providers import UserContext
    return UserContext(
        sub="user-viewer", email="v@t", name="Viewer", role="member",
        agents=[AGENT], agent_roles={AGENT: "viewer"},
    )


def _manager():
    from auth.providers import UserContext
    return UserContext(
        sub="user-manager", email="m@t", name="Manager", role="member",
        agents=[AGENT], agent_roles={AGENT: "manager"},
    )


# ---------------------------------------------------------------------------
# seed_user_setup_file
# ---------------------------------------------------------------------------

class TestSeedUserSetupFile:
    def test_seeds_copy_for_user(self, temp_db):
        from services.community import community_agent_installer as cai
        agent_dir = _mk_agent()
        _set_username("user-viewer", "viewer")
        (agent_dir / "config" / "user-setup.md").write_text("welcome!")

        assert cai.seed_user_setup_file(AGENT, "user-viewer") == 1
        dest = agent_dir / "users" / "viewer" / "context" / "user-setup.md"
        assert dest.read_text() == "welcome!"

    def test_no_canonical_file_is_noop(self, temp_db):
        from services.community import community_agent_installer as cai
        _mk_agent()
        _set_username("user-viewer", "viewer")
        assert cai.seed_user_setup_file(AGENT, "user-viewer") == 0

    def test_existing_copy_not_overwritten(self, temp_db):
        from services.community import community_agent_installer as cai
        agent_dir = _mk_agent()
        _set_username("user-viewer", "viewer")
        (agent_dir / "config" / "user-setup.md").write_text("v2")
        dest = agent_dir / "users" / "viewer" / "context" / "user-setup.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("mid-onboarding notes")

        assert cai.seed_user_setup_file(AGENT, "user-viewer") == 0
        assert dest.read_text() == "mid-onboarding notes"

    def test_reattach_after_completion_drops_tombstone(self, temp_db):
        """A completed user (file deleted with a tombstone) who is removed
        and re-attached is re-onboarded — the fresh seed must drop the old
        delete tombstone or the next sync merge would remove it again."""
        from services.community import community_agent_installer as cai
        agent_dir = _mk_agent()
        _set_username("user-viewer", "viewer")
        (agent_dir / "config" / "user-setup.md").write_text("welcome!")
        rel = "users/viewer/context/user-setup.md"
        file_tombstones_store.record(AGENT, rel, time.time(), origin="test")

        assert cai.seed_user_setup_file(AGENT, "user-viewer") == 1
        assert file_tombstones_store.get(AGENT, rel) is None

    def test_unknown_username_is_noop(self, temp_db):
        from services.community import community_agent_installer as cai
        agent_dir = _mk_agent()
        (agent_dir / "config" / "user-setup.md").write_text("x")
        assert cai.seed_user_setup_file(AGENT, "sub-without-username") == 0


# ---------------------------------------------------------------------------
# on_user_added_to_agent ordering + opt-out
# ---------------------------------------------------------------------------

class TestAttachHookSeeding:
    def test_manual_agent_still_seeds(self, temp_db):
        """The copy runs BEFORE the community-template guard: a manually
        created agent (no template data) participates by just having
        config/user-setup.md."""
        from services.community import community_agent_installer as cai
        agent_dir = _mk_agent()
        _set_username("user-viewer", "viewer")
        (agent_dir / "config" / "user-setup.md").write_text("hello")

        counts = cai.on_user_added_to_agent(AGENT, "user-viewer", "viewer")
        assert counts["user_setup"] == 1
        assert (agent_dir / "users" / "viewer" / "context" / "user-setup.md").is_file()

    def test_reseed_opt_out_skips_user_setup(self, temp_db):
        from services.community import community_agent_installer as cai
        agent_dir = _mk_agent()
        _set_username("user-viewer", "viewer")
        (agent_dir / "config" / "user-setup.md").write_text("hello")

        counts = cai.on_user_added_to_agent(
            AGENT, "user-viewer", "viewer", seed_user_setup=False,
        )
        assert counts["user_setup"] == 0
        assert not (agent_dir / "users" / "viewer" / "context" / "user-setup.md").exists()


# ---------------------------------------------------------------------------
# POST /v1/agents/{name}/complete-setup
# ---------------------------------------------------------------------------

def _call(user_ctx, scope=None, summary=""):
    from api.agents.discovery import complete_agent_setup, CompleteSetupBody
    with patch(
        "services.notifications.notification_manager.fire_notification",
        new=AsyncMock(),
    ):
        return asyncio.run(complete_agent_setup(
            AGENT, CompleteSetupBody(summary=summary, scope=scope), user_ctx,
        ))


class TestCompleteSetupScopes:
    def test_user_scope_deletes_only_callers_file(self, temp_db):
        agent_dir = _mk_agent()
        _set_username("user-viewer", "viewer")
        _set_username("user-manager", "manager")
        for uname in ("viewer", "manager"):
            p = agent_dir / "users" / uname / "context" / "user-setup.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("onboarding")

        res = _call(_viewer(), scope="user")
        assert res["status"] == "user_setup_complete"
        assert res["user_setup_removed"] is True
        assert not (agent_dir / "users" / "viewer" / "context" / "user-setup.md").exists()
        # The other user's onboarding is untouched.
        assert (agent_dir / "users" / "manager" / "context" / "user-setup.md").is_file()
        # Sync tombstone recorded so a satellite copy can't resurrect it.
        assert file_tombstones_store.get(
            AGENT, "users/viewer/context/user-setup.md",
        ) is not None

    def test_user_scope_second_call_is_idempotent(self, temp_db):
        agent_dir = _mk_agent()
        _set_username("user-viewer", "viewer")
        p = agent_dir / "users" / "viewer" / "context" / "user-setup.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("onboarding")

        assert _call(_viewer(), scope="user")["user_setup_removed"] is True
        second = _call(_viewer(), scope="user")
        assert second["status"] == "user_setup_complete"
        assert second["user_setup_removed"] is False

    def test_no_scope_resolves_to_user_file(self, temp_db):
        agent_dir = _mk_agent()
        _set_username("user-viewer", "viewer")
        p = agent_dir / "users" / "viewer" / "context" / "user-setup.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("onboarding")

        res = _call(_viewer())
        assert res["status"] == "user_setup_complete"

    def test_both_files_without_scope_is_400(self, temp_db):
        from fastapi import HTTPException
        agent_dir = _mk_agent()
        _set_username("user-manager", "manager")
        (agent_dir / "config" / "context").mkdir(parents=True, exist_ok=True)
        (agent_dir / "config" / "context" / "setup.md").write_text("agent setup")
        p = agent_dir / "users" / "manager" / "context" / "user-setup.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("user setup")

        with pytest.raises(HTTPException) as exc:
            _call(_manager())
        assert exc.value.status_code == 400

    def test_agent_scope_requires_manager(self, temp_db):
        from fastapi import HTTPException
        agent_dir = _mk_agent()
        _set_username("user-viewer", "viewer")
        (agent_dir / "config" / "context").mkdir(parents=True, exist_ok=True)
        (agent_dir / "config" / "context" / "setup.md").write_text("agent setup")

        with pytest.raises(HTTPException) as exc:
            _call(_viewer(), scope="agent")
        assert exc.value.status_code == 403
        # The file is untouched.
        assert (agent_dir / "config" / "context" / "setup.md").is_file()

    def test_agent_scope_manager_completes_with_tombstone(self, temp_db):
        agent_dir = _mk_agent()
        _set_username("user-manager", "manager")
        (agent_dir / "config" / "context").mkdir(parents=True, exist_ok=True)
        (agent_dir / "config" / "context" / "setup.md").write_text("agent setup")

        res = _call(_manager(), scope="agent")
        assert res["status"] == "completed"
        assert res["setup_md_removed"] is True
        assert not (agent_dir / "config" / "context" / "setup.md").exists()
        assert file_tombstones_store.get(
            AGENT, "config/context/setup.md",
        ) is not None
        # Re-run: already complete, never an error.
        again = _call(_manager(), scope="agent")
        assert again["status"] == "already_complete"

    def test_invalid_scope_is_400(self, temp_db):
        from fastapi import HTTPException
        _mk_agent()
        with pytest.raises(HTTPException) as exc:
            _call(_manager(), scope="bananas")
        assert exc.value.status_code == 400
