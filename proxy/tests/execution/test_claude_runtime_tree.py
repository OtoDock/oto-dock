"""Claude-CLI session runtime-tree carve-out (path_policy_v2).

The session's OWN ``<claude_runtime_root>/<cwd-slug>/<session-id>/`` subtree
is admitted read+write on a remote satellite even in home-only mode; the
root comes from the satellite's capabilities probe and the session id is
stamped centrally by ``session_state.set_session_security``. The critical
property (mirroring test_bg_output_read.py): the allow can never weaken the
credential / .ssh / .env / cross-session denies that run before it.

Run standalone (one file at a time — concurrent pytest deadlocks on schema-init):
    proxy/venv/bin/python -m pytest tests/execution/test_claude_runtime_tree.py -x
"""

import dataclasses

import pytest

from auth.path_policy import SecurityContext, _check_remote_bash_path
from core.session import session_state
from services.path_policy_v2 import (
    PathPolicyContext,
    _is_session_runtime_path,
    context_from_security,
    resolve_path_for_session,
)
from core import placement

SID = "76fe15d2-9b5b-495c-9c6c-42e6626f88f6"
OTHER_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
ROOT = "/tmp/claude-1000"
TREE = f"{ROOT}/-home-dave/{SID}"


_PLACEMENT = placement.PlacementCapabilities(
    kind=placement.KIND_USER_REMOTE, machine_id="m1", home_dir="/home/dave",
    os_user="dave", allow_full_fs=False, agents_dir="/home/dave/.oto-dock/agents",
    os="linux", claude_runtime_root=ROOT,
)


def _ctx(**over):
    kwargs = dict(
        agent_slug="demo",
        role="manager",
        cli_session_id=SID,
        placement=_PLACEMENT,
    )
    kwargs.update(over)
    return PathPolicyContext(**kwargs)


# --------------------------------------------------------------------------- #
# _is_session_runtime_path — the structural check
# --------------------------------------------------------------------------- #

def test_matcher_linux_tree_and_sid_dir_itself():
    ctx = _ctx()
    assert _is_session_runtime_path(f"{TREE}/scratchpad/notes.txt", ctx)
    assert _is_session_runtime_path(f"{TREE}/tasks/abc.output", ctx)
    assert _is_session_runtime_path(TREE, ctx)


def test_matcher_windows_root_case_insensitive():
    ctx = _ctx(placement=dataclasses.replace(
        _PLACEMENT, os="windows",
        claude_runtime_root="c:/Users/f/AppData/Local/Temp/claude",
    ))
    assert _is_session_runtime_path(
        f"c:/Users/f/AppData/Local/Temp/Claude/-c-proj/{SID}/scratchpad/x", ctx,
    )


def test_matcher_rejects_wrong_or_missing_sid():
    ctx = _ctx()
    assert not _is_session_runtime_path(f"{ROOT}/-home-dave/{OTHER_SID}/x", ctx)
    assert not _is_session_runtime_path(f"{ROOT}/-home-dave/no-sid-here/x", ctx)
    assert not _is_session_runtime_path(ROOT, ctx)  # the bare root


def test_matcher_disabled_without_root_or_sid():
    assert not _is_session_runtime_path(f"{TREE}/x", _ctx(placement=dataclasses.replace(_PLACEMENT, claude_runtime_root="")))
    assert not _is_session_runtime_path(f"{TREE}/x", _ctx(cli_session_id=""))


def test_matcher_rejects_outside_root():
    ctx = _ctx()
    # Same shape under a different uid's root — root equality is exact.
    assert not _is_session_runtime_path(f"/tmp/claude-9999/-home-dave/{SID}/x", ctx)
    assert not _is_session_runtime_path(f"/home/dave/claude-1000/{SID}/x", ctx)


# --------------------------------------------------------------------------- #
# resolve_path_for_session — the gate (denial-wins is the load-bearing part)
# --------------------------------------------------------------------------- #

def test_gate_read_and_write_allowed_with_transfer_flags():
    ctx = _ctx()
    r = resolve_path_for_session(ctx, f"{TREE}/scratchpad/notes.txt", writing=False)
    assert r.allowed and r.is_remote_pull and not r.is_remote_push
    assert r.path_ref.kind == "satellite_host"
    w = resolve_path_for_session(ctx, f"{TREE}/scratchpad/notes.txt", writing=True)
    assert w.allowed and w.is_remote_push and not w.is_remote_pull


def test_gate_plain_tmp_still_denied():
    ctx = _ctx()
    for writing in (False, True):
        r = resolve_path_for_session(ctx, "/tmp/evil.txt", writing=writing)
        assert not r.allowed
        assert "outside the OS user's home" in r.error


def test_gate_other_session_and_disabled_carve_denied():
    ctx = _ctx()
    r = resolve_path_for_session(
        ctx, f"{ROOT}/-home-dave/{OTHER_SID}/scratchpad/x", writing=True)
    assert not r.allowed
    r = resolve_path_for_session(
        _ctx(cli_session_id=""), f"{TREE}/scratchpad/x", writing=False)
    assert not r.allowed


def test_gate_protected_paths_inside_tree_stay_denied():
    ctx = _ctx()
    r = resolve_path_for_session(ctx, f"{TREE}/.ssh/id_rsa", writing=False)
    assert not r.allowed
    r = resolve_path_for_session(ctx, f"{TREE}/scratchpad/.env", writing=False)
    assert not r.allowed and ".env" in r.error
    r = resolve_path_for_session(ctx, f"{TREE}/scratchpad/.env", writing=True)
    assert not r.allowed


def test_gate_traversal_collapses_before_matching():
    ctx = _ctx()
    # Collapses to /tmp/evil — outside the tree, denied.
    r = resolve_path_for_session(
        ctx, f"{TREE}/scratchpad/../../../../evil", writing=True)
    assert not r.allowed


def test_gate_bg_output_read_survives_with_carve_disabled():
    # Regression guard: contexts without a cli_session_id (phone, synthetic,
    # pre-upgrade rehydrations) keep the read-only bg-output carve.
    ctx = _ctx(cli_session_id="", placement=dataclasses.replace(_PLACEMENT, claude_runtime_root=""))
    r = resolve_path_for_session(
        ctx, f"/tmp/claude-1000/-home-dave/{SID}/tasks/t1.output", writing=False)
    assert r.allowed and r.is_remote_pull


# --------------------------------------------------------------------------- #
# Threading: SecurityContext → context_from_security → bash gate
# --------------------------------------------------------------------------- #

def _security_ctx(**over):
    kwargs = dict(
        role="manager",
        username="dave",
        agent="demo",
        is_admin_agent=False,
        cli_session_id=SID,
        placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, agents_dir="/home/dave/.oto-dock/agents", home_dir="/home/dave", allow_full_fs=False, claude_runtime_root=ROOT),
    )
    kwargs.update(over)
    return SecurityContext(**kwargs)


def test_context_from_security_copies_carve_fields():
    ctx = context_from_security(_security_ctx())
    assert ctx.placement.claude_runtime_root == ROOT
    assert ctx.cli_session_id == SID


def test_bash_redirect_into_tree_allowed_evil_denied():
    sc = _security_ctx()
    d = _check_remote_bash_path(f"{TREE}/scratchpad/log.txt", sc, writing=True)
    assert d.allowed
    d = _check_remote_bash_path("/tmp/evil.txt", sc, writing=True)
    assert not d.allowed


# --------------------------------------------------------------------------- #
# Central stamp: set_session_security
# --------------------------------------------------------------------------- #

@pytest.fixture
def _no_persist(monkeypatch):
    monkeypatch.setattr(session_state, "_save_session_security", lambda: None)


def test_stamp_valid_uuid(_no_persist):
    session_state.set_session_security(SID, _security_ctx(cli_session_id=""))
    assert session_state.get_session_security(SID).cli_session_id == SID
    session_state._session_security.pop(SID, None)


def test_stamp_rejects_non_uuid(_no_persist):
    session_state.set_session_security("not-a-uuid", _security_ctx(cli_session_id=""))
    assert session_state.get_session_security("not-a-uuid").cli_session_id == ""
    session_state._session_security.pop("not-a-uuid", None)


def test_stamp_preserves_existing_value(_no_persist):
    session_state.set_session_security(SID, _security_ctx(cli_session_id=OTHER_SID))
    assert session_state.get_session_security(SID).cli_session_id == OTHER_SID
    session_state._session_security.pop(SID, None)


def test_security_ctx_serialization_roundtrip():
    sc = _security_ctx()
    data = session_state._serialize_security_ctx(sc)
    back = session_state._deserialize_security_ctx(data)
    assert back.cli_session_id == SID
    assert back.placement.claude_runtime_root == ROOT


def test_live_refresh_replace_preserves_carve_fields():
    sc = _security_ctx()
    replaced = dataclasses.replace(sc, placement=dataclasses.replace(sc.placement, allow_full_fs=True))
    assert replaced.cli_session_id == SID
    assert replaced.placement.claude_runtime_root == ROOT


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
