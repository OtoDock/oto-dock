"""Tests for role-aware resolve_execution_target.

Covers the viewer-on-admin-remote security guard + fallback flag matrix.
"""

from unittest.mock import patch


def test_viewer_forced_to_local_when_agent_has_remote_target(temp_db):
    """A viewer's session on an agent with an admin-configured remote target
    must route to local — no bwrap isolation on remote, so viewer-scope file
    access can't be enforced. Viewer's own user-override is unaffected (see
    test_viewer_own_override_is_honored)."""
    from services.remote.remote_status import resolve_execution_target

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        target, reason = resolve_execution_target(
            "test-agent", user_sub=None, role="viewer",
        )
        assert target == "local"
        assert reason == "viewer-on-admin-remote"


def test_contributor_forced_to_local_like_a_viewer(temp_db):
    """The workspace tier is below manager: a contributor's chat on an
    admin-targeted agent runs locally too, and the reason travels with the
    target so the badge can say so."""
    from services.remote.remote_status import resolve_execution_target

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        target, reason = resolve_execution_target(
            "test-agent", user_sub="pm-1", role="contributor",
        )
        assert target == "local"
        assert reason == "viewer-on-admin-remote"


def test_manager_uses_agent_remote_target_when_online(temp_db):
    """Manager on agent with remote target — no fallback when online."""
    from services.remote.remote_status import resolve_execution_target

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        target, reason = resolve_execution_target(
            "test-agent", user_sub=None, role="manager",
        )
        assert target == "machine-admin"
        assert reason is None


def test_viewer_own_override_is_honored(temp_db):
    """Viewer's user-level override applies even though admin-remote is
    forbidden — viewer owns their hardware, they accept the trade-off."""
    from services.remote import remote_status
    from storage import remote_store

    with patch.object(remote_store, "get_user_remote_target",
                      return_value={"machine_id": "machine-mine"}), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        target, reason = remote_status.resolve_execution_target(
            "test-agent", user_sub="viewer-1", role="viewer",
        )
        assert target == "machine-mine"
        assert reason is None


def test_agent_default_offline_hard_fail(temp_db):
    """Agent-default target offline, fallback-agent-default=false (default) →
    sentinel that warmup detects and rejects."""
    from services.remote import remote_status
    from storage import database as db

    db.set_platform_setting("remote_fallback_agent_default", "0")

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=False):
        target, reason = remote_status.resolve_execution_target(
            "test-agent", user_sub=None, role="manager",
        )
        assert target.startswith("__offline__:")
        assert reason == "agent-default-offline-hard-fail"


def test_agent_default_offline_fallback_to_local(temp_db):
    """Same scenario but with remote_fallback_agent_default=true."""
    from services.remote import remote_status
    from storage import database as db

    db.set_platform_setting("remote_fallback_agent_default", "1")

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable", return_value=False):
        target, reason = remote_status.resolve_execution_target(
            "test-agent", user_sub=None, role="manager",
        )
        assert target == "local"
        assert reason == "agent-default-offline"


def test_user_override_offline_soft_fallback_to_agent_default(temp_db):
    """User's own machine offline + agent default online → runs on agent
    default with fallback_reason='user-override-offline' (default behavior)."""
    from services.remote import remote_status
    from storage import remote_store

    # machine-mine offline; machine-admin online
    reachable_map = {"machine-mine": False, "machine-admin": True}
    with patch.object(remote_store, "get_user_remote_target",
                      return_value={"machine_id": "machine-mine"}), \
         patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch("services.remote.remote_status.is_reachable",
               side_effect=lambda mid: reachable_map.get(mid, False)):
        target, reason = remote_status.resolve_execution_target(
            "test-agent", user_sub="user-1", role="manager",
        )
        assert target == "machine-admin"
        assert reason == "user-override-offline"


def test_user_override_offline_hard_fail_when_fallback_disabled(temp_db):
    """User's machine offline and fallback disabled → sentinel."""
    from services.remote import remote_status
    from storage import database as db, remote_store

    db.set_platform_setting("remote_fallback_user_override", "0")
    with patch.object(remote_store, "get_user_remote_target",
                      return_value={"machine_id": "machine-mine"}), \
         patch("services.remote.remote_status.is_reachable", return_value=False):
        target, reason = remote_status.resolve_execution_target(
            "test-agent", user_sub="user-1", role="manager",
        )
        assert target.startswith("__offline__:")
        assert reason == "user-override-offline-hard-fail"


# ---------------------------------------------------------------------------
# "Every role of its agents runs here": the admin machine's own switch
# ---------------------------------------------------------------------------

_ON = {"id": "machine-admin", "pairing_scope": "admin", "all_roles_run_here": True}
_OFF = dict(_ON, all_roles_run_here=False)


def _resolve(role, *, machine, user_sub="person-1", reachable=True, agent_target="machine-admin",
             user_target=None):
    from services.remote import remote_status
    from storage import remote_store

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": agent_target}), \
         patch.object(remote_store, "get_remote_machine",
                      side_effect=lambda mid: machine if machine and mid == machine["id"] else None), \
         patch.object(remote_store, "get_user_remote_target", return_value=user_target), \
         patch("services.remote.remote_status.is_reachable",
               side_effect=reachable if callable(reachable) else (lambda mid: reachable)):
        return remote_status.resolve_execution_target("test-agent", user_sub=user_sub, role=role)


def test_every_role_below_the_owner_tier_follows_the_switch(temp_db, caplog):
    """Off, a viewer's, a contributor's and an editor's chat run on the
    platform host; on, they run on the agent's default machine, the reason
    names the switch and the log says so. A manager is the same either way."""
    import logging
    for role in ("viewer", "contributor", "editor"):
        assert _resolve(role, machine=_OFF) == ("local", "viewer-on-admin-remote")
        with caplog.at_level(logging.INFO, logger="otodock.remote-status"):
            assert _resolve(role, machine=_ON) == ("machine-admin", "all-roles-on-admin-remote")
        assert (f"test-agent as {role} runs on machine- by the machine's every-role switch"
                in caplog.text)
    for role in ("manager", "admin"):
        assert _resolve(role, machine=_OFF) == ("machine-admin", None)
        assert _resolve(role, machine=_ON) == ("machine-admin", None)


def test_a_session_with_no_person_never_follows_the_switch(temp_db):
    """An outside caller (a phone call's external identity: the viewer
    role, no person) stays on the platform host with the switch on."""
    assert _resolve("viewer", machine=_ON, user_sub=None) == ("local", "viewer-on-admin-remote")
    assert _resolve("viewer", machine=_ON, user_sub="") == ("local", "viewer-on-admin-remote")


def test_an_external_call_stays_on_the_platform_with_the_switch_on(temp_db):
    from core.config.phone_config_builder import resolve_phone_execution_target
    from services.phone import phone_identity as pi
    from storage import remote_store

    with patch("storage.agents.agent_store.get_agent",
               return_value={"execution_target": "machine-admin"}), \
         patch.object(remote_store, "get_remote_machine", return_value=_ON), \
         patch("services.remote.remote_status.is_reachable", return_value=True):
        assert resolve_phone_execution_target("test-agent") == "local"
        assert resolve_phone_execution_target("test-agent", role=pi.EXTERNAL_ROUTE_ROLE) == "local"
        # A user route is a platform person: their capped role follows the switch.
        assert resolve_phone_execution_target(
            "test-agent", role="viewer", user_sub="person-1") == "machine-admin"


def test_the_switch_admits_only_its_own_admin_machine(temp_db):
    """A personal row with the flag set, or the flag on a machine that is
    not the agent's default, admits nothing."""
    personal = dict(_ON, pairing_scope="user", registered_by="person-1")
    assert _resolve("viewer", machine=personal) == ("local", "viewer-on-admin-remote")
    other = dict(_ON, id="machine-other")
    assert _resolve("viewer", machine=other) == ("local", "viewer-on-admin-remote")
    assert _resolve("viewer", machine=None) == ("local", "viewer-on-admin-remote")


def test_the_switch_keeps_the_offline_rules(temp_db):
    """On and offline: the owner's rules, a sentinel by default and the
    platform host when the agent-default fallback is on."""
    from storage import database as db

    target, reason = _resolve("contributor", machine=_ON, reachable=False)
    assert target == "__offline__:machine-admin" and reason == "agent-default-offline-hard-fail"
    db.set_platform_setting("remote_fallback_agent_default", "1")
    assert _resolve("contributor", machine=_ON, reachable=False) == ("local", "agent-default-offline")


def test_the_persons_own_machine_still_comes_first(temp_db):
    mine = {"machine_id": "machine-mine"}
    assert _resolve("viewer", machine=_ON, user_target=mine) == ("machine-mine", None)
    # Their machine offline falls through to the switched default, and the
    # reason says their own machine was offline.
    reach = {"machine-mine": False, "machine-admin": True}
    assert _resolve("viewer", machine=_ON, user_target=mine, reachable=reach.get) == (
        "machine-admin", "user-override-offline")
