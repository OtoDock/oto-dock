"""The read-only hook bind (the access audit's H5): the hook scripts and the
stdio interceptor a session's CLI state dir holds are bound read-only over
their copies, so a session cannot rewrite the hook that judges its own
tools. Satellites run no bwrap and are unchanged."""

from __future__ import annotations

import subprocess

from core.sandbox.sandbox import SandboxBuilder
from tests.execution.test_sandbox import _make_config, _needs_netns, tmp_agents  # noqa: F401

_CLAUDE = ".claude"
_CODEX = ".codex"


def _hooks_in(d):
    from core.sandbox.session_config_dir import HOOK_SCRIPTS
    d.mkdir(parents=True, exist_ok=True)
    for name in (*HOOK_SCRIPTS, "stdio_path_interceptor.py"):
        (d / name).write_text("# hook\n")


def _ro_binds(cmd):
    return {b: c for a, b, c in zip(cmd, cmd[1:], cmd[2:]) if a == "--ro-bind"}


def test_a_users_hook_files_are_bound_read_only_over_its_rw_dir(tmp_agents):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    state = agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE
    _hooks_in(state)
    cmd = SandboxBuilder(cfg).build_command_prefix(["claude"])
    ro = _ro_binds(cmd)
    for name in ("permission_gate.py", "stop_tracker.py", "stdio_path_interceptor.py"):
        assert ro[str(state / name)] == f"/users/alice/{_CLAUDE}/{name}"
    # The file binds come after the RW dir bind (a later bind wins).
    assert cmd.index(str(state / "permission_gate.py")) > cmd.index(str(state))


def test_the_agent_scopes_hooks_inside_workspace(tmp_agents):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="manager", username="")
    state = agents_dir / "personal-assistant" / "workspace" / _CODEX
    _hooks_in(state)
    cmd = SandboxBuilder(cfg).build_command_prefix(["codex"])
    ro = _ro_binds(cmd)
    assert ro[str(state / "permission_gate.py")] == f"/workspace/{_CODEX}/permission_gate.py"
    # The state dir inside /workspace is a mount of its own (a mountpoint
    # cannot be renamed away), bound before its files.
    table = SandboxBuilder(cfg).workspace_mount_table()
    own = [i for i, m in enumerate(table) if m.sandbox == f"/workspace/{_CODEX}"]
    first_file = next(i for i, m in enumerate(table)
                      if m.sandbox.startswith(f"/workspace/{_CODEX}/"))
    assert own and table[own[0]].rw and table[own[0]].host == str(state)
    assert own[0] < first_file


def test_a_state_dir_without_hooks_takes_no_bind(tmp_agents):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="manager", username="")
    state = agents_dir / "personal-assistant" / "workspace" / _CLAUDE
    state.mkdir(parents=True, exist_ok=True)
    for f in state.glob("*.py"):
        f.unlink()
    table = SandboxBuilder(cfg).workspace_mount_table()
    assert not [m for m in table if m.sandbox.startswith(f"/workspace/{_CLAUDE}")]


def test_a_masked_state_dir_takes_no_file_bind(tmp_agents):  # noqa: F811
    """A user-scope session with the shared workspace sees the agent's own
    CLI state masked: a file bind into the empty mask would fail the start."""
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    _hooks_in(agents_dir / "personal-assistant" / "workspace" / _CLAUDE)
    table = SandboxBuilder(cfg).workspace_mount_table()
    assert not [m for m in table if m.sandbox.startswith(f"/workspace/{_CLAUDE}/")]


def test_a_planted_link_is_skipped(tmp_agents, tmp_path):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    state = agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE
    state.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.py"
    outside.write_text("x")
    (state / "permission_gate.py").symlink_to(outside)
    table = SandboxBuilder(cfg).workspace_mount_table()
    assert [m for m in table if m.sandbox.startswith(f"/users/alice/{_CLAUDE}/")] == []


@_needs_netns
def test_the_hook_reaches_the_session_read_only_through_its_held_handle(tmp_agents):  # noqa: F811
    """End to end through the launcher: the hook file is bound from the
    descriptor the shim held (F68), readable and not writable inside."""
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    _hooks_in(agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE)
    gate = f"/users/alice/{_CLAUDE}/permission_gate.py"
    probe = (f"print(open({gate!r}).read().strip())\n"
             "try:\n"
             f"    open({gate!r}, 'a').write('x')\n"
             "    print('written')\n"
             "except OSError:\n"
             "    print('read-only')\n")
    cmd = SandboxBuilder(cfg).build_command_prefix(["python3", "-c", probe])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert out.stdout.split() == ["#", "hook", "read-only"], (out.stdout, out.stderr)


def test_the_table_the_direct_resolver_reads_has_the_hook_read_only(tmp_agents):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    _hooks_in(agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE)
    table = SandboxBuilder(cfg).workspace_mount_table()
    hook = [m for m in table if m.sandbox == f"/users/alice/{_CLAUDE}/permission_gate.py"]
    assert hook and hook[0].rw is False
