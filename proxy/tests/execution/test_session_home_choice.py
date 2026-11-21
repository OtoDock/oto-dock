"""Which sessions run from a home of their own
(``session_config_dir.session_home_for``) and the key the pool reads them by
(``pool_bindings.scope_key_for``)."""

from types import SimpleNamespace

import pytest

from core import placement
from core.sandbox.session_config_dir import session_home_for
from services.engines import pool_bindings

SID = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
STORE = "/agents/a/workspace/.claude"


BOTH = frozenset({".claude", ".codex"})


def _ctx(kind=placement.KIND_USER_REMOTE, homes=BOTH, **extra):
    pl = placement.PlacementCapabilities(kind=kind, machine_id="m-1", session_homes=frozenset(homes or ()))
    return SimpleNamespace(placement=pl, external_home="", wrapup="", open_line=False,
                           read_only=False, **extra)


def test_a_machine_that_makes_homes_gives_one():
    for kind in (placement.KIND_USER_REMOTE, placement.KIND_ADMIN_REMOTE, placement.KIND_COMPUTE):
        assert session_home_for(STORE, SID, execution_path="claude-code-cli",
                                ctx=_ctx(kind)) == f"{STORE}/own/{SID}"


@pytest.mark.parametrize("why, ctx, path, sid", [
    ("a satellite that makes no home", _ctx(homes=()), "claude-code-cli", SID),
    ("the platform's own sandbox", _ctx(placement.KIND_LOCAL), "claude-code-cli", SID),
    ("a Codex session on a machine that makes Claude Code's homes alone", _ctx(homes={".claude"}), "codex-cli", SID),
    ("no engine folder at all", _ctx(), "direct-llm", SID),
    ("a session id a home cannot carry", _ctx(), "claude-code-cli", "sess-1"),
])
def test_no_home(why, ctx, path, sid):
    assert session_home_for(STORE, sid, execution_path=path, ctx=ctx) == "", why


@pytest.mark.parametrize("field, value", [
    ("external_home", "/agents/a/externals/phone/x"), ("wrapup", "after"), ("open_line", True)])
def test_a_scratch_home_gets_none(field, value):
    ctx = _ctx()
    setattr(ctx, field, value)
    assert session_home_for(STORE, SID, execution_path="claude-code-cli", ctx=ctx) == ""


def test_the_key_is_the_homes_when_there_is_one():
    assert pool_bindings.scope_key_for("m-1", STORE) == f"m-1:{STORE}"
    assert pool_bindings.scope_key_for("m-1", STORE, f"{STORE}/own/{SID}") == f"m-1:{STORE}/own/{SID}"
    assert pool_bindings.scope_key_for("", STORE) == f"local:{STORE}"
    # Two sessions of one scope with homes never share a key: no pin, no group.
    other = "11111111-2222-3333-4444-555555555555"
    assert (pool_bindings.scope_key_for("m-1", STORE, f"{STORE}/own/{SID}")
            != pool_bindings.scope_key_for("m-1", STORE, f"{STORE}/own/{other}"))


def test_the_machines_report_reaches_its_placement():
    """The engine folders a satellite makes homes in: its ``session_home_dirs``,
    else Claude Code's alone for a report of ``session_homes`` (0.5.191 and
    older), else none."""
    def homes(kind, caps):
        return placement.from_machine(kind, {"id": "m-1", "name": "laptop", "capabilities": caps}).session_homes
    claude = frozenset({".claude"})
    assert homes(placement.KIND_USER_REMOTE, '{"os": "linux", "session_homes": true}') == claude
    assert homes(placement.KIND_USER_REMOTE,
                 '{"session_homes": true, "session_home_dirs": [".claude", ".codex"]}') == BOTH
    assert homes(placement.KIND_USER_REMOTE, '{"os": "linux"}') == frozenset()
    assert homes(placement.KIND_USER_REMOTE, '{"session_homes": "yes"}') == frozenset()
    assert homes(placement.KIND_USER_REMOTE, '{"session_home_dirs": ".codex"}') == frozenset()
    assert homes(placement.KIND_COMPUTE, '{"compute": true, "session_homes": true}') == claude
    assert homes(placement.KIND_COMPUTE, '{"session_home_dirs": [".claude", ".codex"]}') == BOTH
    assert homes(placement.KIND_COMPUTE, "{") == frozenset()


def test_a_machine_makes_homes_only_in_the_engine_folders_it_reports():
    """A satellite that makes Claude Code's homes alone (0.5.191 and older) runs
    every other engine from the scope's folder: the start asks no home of it."""
    claude_only, codex_only = _ctx(homes={".claude"}), _ctx(homes={".codex"})
    assert session_home_for(STORE, SID, execution_path="claude-code-cli", ctx=claude_only) == f"{STORE}/own/{SID}"
    assert session_home_for(STORE, SID, execution_path="claude-code-cli", ctx=codex_only) == ""
