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


def test_a_declared_structured_tool_is_allowed_without_a_net():
    decision, _ = check_tool_access("Agent", {"description": "rm -rf / example", "prompt": "x"}, _ctx())
    assert decision.allowed
