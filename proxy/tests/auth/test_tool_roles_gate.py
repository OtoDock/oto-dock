"""The permission gate keyed on tool ROLES (core-seams phase 2): an
undeclared tool with a command payload is refused on every placement — ahead
of the admin fast path — while an undeclared structured tool keeps the
catastrophe net; the shells route by dialect; a notebook edit is a write
under its own key; a patch under ``command`` is a patch, not a shell."""

from __future__ import annotations

import pytest

from auth.path_policy import SecurityContext, check_tool_access
from core import placement


def _ctx(**over) -> SecurityContext:
    base = dict(role="manager", username="alice", agent="pa", is_admin_agent=False)
    base.update(over)
    return SecurityContext(**base)


@pytest.mark.parametrize("ctx", [
    _ctx(),
    _ctx(role="admin", is_admin_agent=True),
    _ctx(placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m1", home_dir="/home/u")),
    _ctx(role="admin", is_admin_agent=True,
         placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m1", home_dir="/home/u")),
], ids=["local", "admin-on-admin", "remote", "admin-remote"])
@pytest.mark.parametrize("key", ["command", "cmd", "script"])
def test_an_undeclared_command_tool_is_refused_everywhere(ctx, key):
    decision, _ = check_tool_access("RunThing", {key: "ls -la"}, ctx)
    assert not decision.allowed
    assert "core/events/tool_roles.py" in decision.reason


def test_an_undeclared_structured_tool_keeps_the_catastrophe_net():
    ok, _ = check_tool_access("ListMcpResourcesTool", {"server": "x"}, _ctx())
    assert ok.allowed
    bad, _ = check_tool_access("ListMcpResourcesTool", {"note": "rm -rf /"}, _ctx())
    assert not bad.allowed and "dangerous" in bad.reason


def test_the_shells_route_by_dialect():
    posix, _ = check_tool_access("Monitor", {"command": "rm -rf / --no-preserve-root"}, _ctx())
    assert not posix.allowed
    ps, _ = check_tool_access("PowerShell", {"command": "Remove-Item -Recurse -Force C:\\"}, _ctx())
    assert not ps.allowed
    empty, _ = check_tool_access("Bash", {"cmd": "ls"}, _ctx())
    assert not empty.allowed  # a shell with no ``command`` is an empty command


def test_a_notebook_edit_is_a_write_under_its_own_key(monkeypatch):
    from auth import path_policy
    seen: dict = {}

    def _spy(raw_path, ctx, *, writing):
        seen["raw_path"], seen["writing"] = raw_path, writing
        return path_policy._ALLOW
    monkeypatch.setattr(path_policy, "_check_path_arg", _spy)
    decision, _ = check_tool_access("NotebookEdit", {"notebook_path": "/workspace/n.ipynb"}, _ctx())
    assert decision.allowed and seen == {"raw_path": "/workspace/n.ipynb", "writing": True}


def test_a_patch_under_command_is_a_patch_not_a_shell(monkeypatch):
    from auth import path_policy
    called: list = []
    monkeypatch.setattr(path_policy, "_check_apply_patch",
                        lambda tool_input, ctx: called.append(tool_input) or path_policy._ALLOW)
    decision, _ = check_tool_access("apply_patch", {"command": "*** Begin Patch\n*** End Patch"}, _ctx())
    assert decision.allowed and called and "Begin Patch" in called[0]["command"]


def test_a_codex_multi_file_change_checks_every_path(monkeypatch):
    from auth import path_policy
    seen: list = []

    def _spy(raw_path, ctx, *, writing):
        seen.append((raw_path, writing))
        return path_policy._ALLOW
    monkeypatch.setattr(path_policy, "_check_path_arg", _spy)
    decision, _ = check_tool_access(
        "Write", {"file_path": "/workspace/a", "_codex_paths": ["/workspace/a", "/workspace/b"]}, _ctx(),
    )
    assert decision.allowed
    assert seen == [("/workspace/b", True), ("/workspace/a", True)]


_EVERY_PLACE = [
    _ctx(),
    _ctx(role="admin", is_admin_agent=True),
    _ctx(placement=placement.PlacementCapabilities(kind=placement.KIND_ADMIN_REMOTE, machine_id="m1", home_dir="/home/u")),
    _ctx(role="admin", is_admin_agent=True,
         placement=placement.PlacementCapabilities(kind=placement.KIND_USER_REMOTE, machine_id="m1", home_dir="/home/u")),
]
_EVERY_PLACE_IDS = ["local", "admin-on-admin", "remote", "admin-remote"]


@pytest.mark.parametrize("ctx", _EVERY_PLACE, ids=_EVERY_PLACE_IDS)
@pytest.mark.parametrize("tool, tool_input", [
    # A Codex change whose approval reached the gate with no path to it (a
    # lean approval in a replay's pause): it was allowed with no path check.
    ("Write", {"file_path": "", "_codex_paths": []}),
    ("Write", {}),
    ("Edit", {"file_path": ""}),
    ("MultiEdit", {"file_path": 3}),
    ("NotebookEdit", {"notebook_path": ""}),
], ids=["codex-lean", "write-none", "edit-empty", "multiedit-no-string", "notebook-empty"])
def test_a_write_that_names_no_file_is_refused_everywhere(ctx, tool, tool_input):
    """The path is all the gate checks of a write: with none there is nothing
    to check, so the call is refused, ahead of the admin fast path, with the
    sentence the card shows."""
    decision, _ = check_tool_access(tool, tool_input, ctx)
    assert not decision.allowed
    assert decision.reason == ("Write denied: this change names no file, so it cannot be checked. "
                               "Try it again.")


@pytest.mark.parametrize("ctx", _EVERY_PLACE, ids=_EVERY_PLACE_IDS)
@pytest.mark.parametrize("extra", [[""], [None], [["x"]], [3]], ids=["empty", "none", "list", "number"])
def test_a_codex_change_with_an_extra_file_that_names_nothing_is_refused(ctx, extra):
    """Every file of a change takes the path rule. An extra entry that is no
    path was skipped, so the change went on with that entry unchecked."""
    decision, _ = check_tool_access(
        "Write", {"file_path": "/workspace/a", "_codex_paths": ["/workspace/a", *extra]}, ctx)
    assert not decision.allowed and decision.reason == path_policy_reason()


@pytest.mark.parametrize("ctx", _EVERY_PLACE, ids=_EVERY_PLACE_IDS)
def test_a_codex_change_whose_file_list_is_not_a_list_is_refused(ctx):
    decision, _ = check_tool_access("Write", {"file_path": "/workspace/a", "_codex_paths": "/etc/passwd"}, ctx)
    assert not decision.allowed and decision.reason == path_policy_reason()


@pytest.mark.parametrize("ctx", _EVERY_PLACE, ids=_EVERY_PLACE_IDS)
@pytest.mark.parametrize("tool, tool_input", [
    ("Write", ["/workspace/a"]), ("Read", "/etc/passwd"), ("Bash", None), ("mcp__file-tools__write_docx", ["x"]),
], ids=["write-list", "read-string", "bash-none", "mcp-list"])
def test_a_call_whose_input_is_not_an_object_is_refused(ctx, tool, tool_input):
    """No engine sends one. It raised inside the rule, and each caller ended
    the call as an error (the hook route's model and the Direct turn's
    stream error): a refusal now, with a sentence, for every tool."""
    from auth.path_policy import NOT_AN_OBJECT_REASON
    decision, _ = check_tool_access(tool, tool_input, ctx)
    assert not decision.allowed and decision.reason == NOT_AN_OBJECT_REASON


def test_a_direct_builtin_whose_input_is_not_an_object_is_denied_not_raised(monkeypatch):
    from auth.path_policy import NOT_AN_OBJECT_REASON
    from core.layers.direct import builtins
    from core.session import session_state
    monkeypatch.setattr(session_state, "get_session_security", lambda sid: _ctx())
    name = next(n for n, b in builtins._REGISTRY.items() if b.file_path_tool)
    session = type("S", (), {"session_id": "s-obj"})()
    assert builtins.gate(session, {"name": name, "input": ["/workspace/a"]}, "dontAsk") == ("deny", NOT_AN_OBJECT_REASON)


def path_policy_reason() -> str:
    from auth.path_policy import NO_PATH_WRITE_REASON
    return NO_PATH_WRITE_REASON


@pytest.mark.parametrize("ctx", _EVERY_PLACE, ids=_EVERY_PLACE_IDS)
def test_a_read_or_a_search_that_names_no_path_is_the_tools_own_error(ctx):
    for tool, tool_input in (("Read", {"file_path": ""}), ("Glob", {"pattern": "*.md"}),
                             ("Grep", {"pattern": "x", "path": ""})):
        decision, _ = check_tool_access(tool, tool_input, ctx)
        assert decision.allowed, tool


def test_a_declared_structured_tool_is_allowed_without_a_net():
    decision, _ = check_tool_access("Agent", {"description": "rm -rf / example", "prompt": "x"}, _ctx())
    assert decision.allowed
