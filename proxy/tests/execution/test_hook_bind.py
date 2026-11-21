"""The read-only hook bind (the access audit's H5): the hook scripts and the
stdio interceptor a session's CLI state dir holds are bound read-only over
their copies, so a session cannot rewrite the hook that judges its own
tools. The binds are rows of the session's sandbox description, read here
from the command a machine renders it into (``test_sandbox._rendered``)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from core.sandbox.sandbox import SandboxBuilder
from tests.execution.test_sandbox import (  # noqa: F401
    _make_config,
    _needs_netns,
    _rendered,
    tmp_agents,
)

_CLAUDE = ".claude"
_CODEX = ".codex"


def _hooks_in(d, wrappers=True):
    from core.sandbox.session_config_dir import HOOK_SCRIPTS, write_command_wrappers
    d.mkdir(parents=True, exist_ok=True)
    for name in (*HOOK_SCRIPTS, "stdio_path_interceptor.py"):
        (d / name).write_text("# hook\n")
    if wrappers:
        write_command_wrappers(d)


def _ro_binds(cmd):
    return {b: c for a, b, c in zip(cmd, cmd[1:], cmd[2:], strict=False) if a == "--ro-bind"}


def test_a_users_hook_files_are_bound_read_only_over_its_rw_dir(tmp_agents):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    state = agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE
    _hooks_in(state)
    cmd = _rendered(SandboxBuilder(cfg), ["claude"])
    ro = _ro_binds(cmd)
    for name in ("permission_gate.py", "stop_tracker.py", "credential_helper.py",
                 "stdio_path_interceptor.py"):
        assert ro[str(state / name)] == f"/users/alice/{_CLAUDE}/{name}"
    # The file binds come after the RW dir bind (a later bind wins).
    assert cmd.index(str(state / "permission_gate.py")) > cmd.index(str(state))
    # The command wrappers' directory, first on the session's PATH, is bound
    # read-only as a whole: nothing can be planted beside the wrappers.
    assert ro[str(state / "bin")] == f"/users/alice/{_CLAUDE}/bin"
    assert cmd.index(str(state / "bin")) > cmd.index(str(state))
    env = SandboxBuilder(cfg).get_env_overrides()
    assert env["PATH"].startswith(f"/users/alice/{_CLAUDE}/bin:/usr/local/sbin:")
    assert env["OTO_HOOKS_DIR"] == f"/users/alice/{_CLAUDE}"
    assert env["OTO_HOOK_PY"].startswith("/")


def test_the_file_that_names_the_hooks_is_bound_read_only_by_its_dir(tmp_agents):  # noqa: F811
    """``settings.json`` in a ``.claude`` dir and ``hooks.json`` in a
    ``.codex`` dir name the hooks: rewriting either would drop the gate. Each
    dir binds only its own engine's file; one planted under the other
    engine's name is left alone (no CLI reads it)."""
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    user = agents_dir / "personal-assistant" / "users" / "alice"
    for sub, own, other in ((_CLAUDE, "settings.json", "hooks.json"),
                            (_CODEX, "hooks.json", "settings.json")):
        _hooks_in(user / sub)
        (user / sub / own).write_text("{}\n")
        (user / sub / other).write_text("{}\n")
    ro = _ro_binds(_rendered(SandboxBuilder(cfg), ["claude"]))
    assert ro[str(user / _CLAUDE / "settings.json")] == f"/users/alice/{_CLAUDE}/settings.json"
    assert ro[str(user / _CODEX / "hooks.json")] == f"/users/alice/{_CODEX}/hooks.json"
    assert str(user / _CLAUDE / "hooks.json") not in ro
    assert str(user / _CODEX / "settings.json") not in ro


def test_a_linked_settings_file_takes_no_bind(tmp_agents, tmp_path):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    state = agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE
    _hooks_in(state)
    outside = tmp_path / "settings.json"
    outside.write_text("{}\n")
    (state / "settings.json").symlink_to(outside)
    ro = _ro_binds(_rendered(SandboxBuilder(cfg), ["claude"]))
    assert str(state / "settings.json") not in ro
    assert str(outside) not in ro


def test_a_planted_link_at_the_wrapper_directory_takes_no_bind(tmp_agents, tmp_path):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    state = agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE
    _hooks_in(state, wrappers=False)
    (state / "bin").symlink_to(tmp_path)
    cmd = _rendered(SandboxBuilder(cfg), ["claude"])
    assert str(state / "bin") not in _ro_binds(cmd)


def test_the_agent_scopes_hooks_inside_workspace(tmp_agents):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="manager", username="")
    state = agents_dir / "personal-assistant" / "workspace" / _CODEX
    _hooks_in(state)
    cmd = _rendered(SandboxBuilder(cfg), ["codex"])
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
    cmd = _rendered(SandboxBuilder(cfg), ["python3", "-c", probe])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert out.stdout.split() == ["#", "hook", "read-only"], (out.stdout, out.stderr)


@_needs_netns
def test_the_settings_file_is_read_only_inside_and_its_dir_stays_writable(tmp_agents):  # noqa: F811
    """End to end: a write to ``settings.json`` fails in the session (a
    rename onto it too, the CLI's own way of writing it), while the CLI
    keeps writing its other state beside it."""
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    state = agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE
    _hooks_in(state)
    (state / "settings.json").write_text('{"hooks": {}}\n')
    d = f"/users/alice/{_CLAUDE}"
    probe = ("import os\n"
             "def attempt(f):\n"
             "    try:\n"
             "        f()\n"
             "        print('written')\n"
             "    except OSError:\n"
             "        print('refused')\n"
             f"attempt(lambda: open({d + '/settings.json'!r}, 'a').write('x'))\n"
             f"open({d + '/new.tmp'!r}, 'w').write('{{}}')\n"
             f"attempt(lambda: os.replace({d + '/new.tmp'!r}, {d + '/settings.json'!r}))\n"
             f"attempt(lambda: open({d + '/state.json'!r}, 'w').write('{{}}'))\n")
    cmd = _rendered(SandboxBuilder(cfg), ["python3", "-c", probe])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert out.stdout.split() == ["refused", "refused", "written"], (out.stdout, out.stderr)
    assert (state / "settings.json").read_text() == '{"hooks": {}}\n'


def test_the_table_the_direct_resolver_reads_has_the_hook_read_only(tmp_agents):  # noqa: F811
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    _hooks_in(agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE)
    table = SandboxBuilder(cfg).workspace_mount_table()
    hook = [m for m in table if m.sandbox == f"/users/alice/{_CLAUDE}/permission_gate.py"]
    assert hook and hook[0].rw is False


def test_the_files_bound_read_only_are_the_hooks_the_interceptor_and_the_hooks_list(tmp_agents):  # noqa: F811
    """The files of a writable state dir bound read-only over their copies
    are the hook scripts, the stdio interceptor and the file that names the
    hooks, and nothing else: a trust bundle an earlier release wrote there
    (``exit-ca.pem``) is read by no session now and takes no bind."""
    from core.sandbox.session_config_dir import HOOK_SCRIPTS
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    state = agents_dir / "personal-assistant" / "users" / "alice" / _CLAUDE
    _hooks_in(state)
    (state / "settings.json").write_text("{}\n")
    (state / "exit-ca.pem").write_text("-----BEGIN CERTIFICATE-----\n")
    ro = _ro_binds(_rendered(SandboxBuilder(cfg), ["claude"]))
    files = {src for src in ro if Path(src).parent == state and Path(src).is_file()}
    assert files == {str(state / name) for name in (*HOOK_SCRIPTS, "stdio_path_interceptor.py", "settings.json")}
    assert str(state / "exit-ca.pem") not in ro


def test_a_codex_homes_instruction_names_are_masked_read_only(tmp_agents):  # noqa: F811
    """Codex reads ``AGENTS.md`` and ``AGENTS.override.md`` in its home for
    every session of the home: each name takes the null device read-only (a
    file mask, so its mountpoint is an empty file a build without the mask
    can still write), a dir planted there is removed first, and a
    ``.claude`` dir takes none."""
    import os
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    user = agents_dir / "personal-assistant" / "users" / "alice"
    _hooks_in(user / _CODEX)
    _hooks_in(user / _CLAUDE)
    (user / _CODEX / "AGENTS.override.md").mkdir()
    cmd = _rendered(SandboxBuilder(cfg), ["codex"])
    masks = [cmd[i + 2] for i in range(len(cmd) - 2)
             if cmd[i] == "--ro-bind" and cmd[i + 1] == os.devnull]
    assert f"/users/alice/{_CODEX}/AGENTS.md" in masks
    assert f"/users/alice/{_CODEX}/AGENTS.override.md" in masks
    assert not [m for m in masks if f"/{_CLAUDE}/" in m]
    # The mountpoints: empty files the owner can write (bwrap would make them
    # read-only, and a build without the mask writes AGENTS.md in place).
    for name in ("AGENTS.md", "AGENTS.override.md"):
        f = user / _CODEX / name
        assert f.is_file() and f.stat().st_size == 0 and f.stat().st_mode & 0o777 == 0o600
    (user / _CODEX / "AGENTS.md").write_text("an older build's text")
    # After the dir bind of the home (a later bind wins).
    assert cmd.index(f"/users/alice/{_CODEX}/AGENTS.md") > cmd.index(f"/users/alice/{_CODEX}")


def test_a_mask_renders_as_the_null_device_on_any_host():
    import os

    from core.sandbox import sandbox_render as render
    from core.sandbox.sandbox import Mount, SandboxBuilder
    b = SandboxBuilder._bind_of(Mount(os.devnull, "/users/a/.codex/AGENTS.md", False), "/agents/a")
    assert (b.root, b.rel, b.rw) == (render.ROOT_NULL, "", False)
    host = render.RenderHost.__new__(render.RenderHost)
    assert render.source_of(b, host, "/agents/a") == os.devnull


@_needs_netns
def test_a_session_cannot_leave_instructions_for_its_neighbours(tmp_agents):  # noqa: F811
    """End to end: inside the sandbox neither name can be written, replaced
    or removed and neither is a file Codex reads; nothing reaches the home on
    the host, and what stays there is an empty file."""
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    home = agents_dir / "personal-assistant" / "users" / "alice" / _CODEX
    _hooks_in(home)
    d = f"/users/alice/{_CODEX}"
    probe = ("import os\n"
             "def attempt(f):\n"
             "    try:\n"
             "        f()\n"
             "        print('written')\n"
             "    except OSError:\n"
             "        print('refused')\n"
             f"for name in ('AGENTS.md', 'AGENTS.override.md'):\n"
             f"    p = {d!r} + '/' + name\n"
             "    attempt(lambda: open(p, 'w').write('obey me'))\n"
             f"    open({d!r} + '/new.md', 'w').write('obey me')\n"
             f"    attempt(lambda: os.replace({d!r} + '/new.md', p))\n"
             "    attempt(lambda: os.unlink(p))\n"
             "    print('file' if os.path.isfile(p) else 'skipped')\n"
             f"attempt(lambda: open({d!r} + '/state.json', 'w').write('{{}}'))\n")
    cmd = _rendered(SandboxBuilder(cfg), ["python3", "-c", probe])
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    # Codex skips a name that is not a regular file (``codex-home`` instructions).
    assert out.stdout.split() == ["refused", "refused", "refused", "skipped"] * 2 + ["written"], (
        out.stdout, out.stderr)
    for name in ("AGENTS.md", "AGENTS.override.md"):
        assert not (home / name).exists() or (home / name).stat().st_size == 0


def test_a_fifo_at_a_masked_name_never_hangs_the_build(tmp_agents):  # noqa: F811
    """A FIFO planted at ``AGENTS.md`` (by a session that ran before the
    mask, or in a tree a build without it wrote) is removed like any name
    that is not a regular file; the build never waits on it."""
    import os
    import threading
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    home = agents_dir / "personal-assistant" / "users" / "alice" / _CODEX
    _hooks_in(home)
    fifo = home / "AGENTS.md"
    os.mkfifo(fifo)
    built: list = []
    worker = threading.Thread(target=lambda: built.append(_rendered(SandboxBuilder(cfg), ["codex"])),
                              daemon=True)
    worker.start()
    worker.join(timeout=10)
    if worker.is_alive():
        # Release the stuck open so the thread ends, then fail.
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
        worker.join(timeout=5)
        raise AssertionError("the sandbox build waited on a FIFO at a masked name")
    assert built and fifo.is_file() and not fifo.is_symlink() and fifo.stat().st_size == 0


def test_a_codex_home_swapped_for_a_link_after_its_check_is_never_written(tmp_agents, tmp_path, monkeypatch):  # noqa: F811
    """The masks' file work opens beneath the agent's real folder: a home
    swapped for a link after the build checked it gets no file made or
    removed through the link, and its masks are bound all the same."""
    import os
    agents_dir, mcps_dir = tmp_agents
    cfg = _make_config(agents_dir, mcps_dir, role="editor")
    home = agents_dir / "personal-assistant" / "users" / "alice" / _CODEX
    _hooks_in(home)
    out = tmp_path / "outside"
    (out / "AGENTS.override.md").mkdir(parents=True)
    real = SandboxBuilder._shared_text_masks

    def swapped(*args):
        home.rename(home.with_name(".codex-moved"))
        home.symlink_to(out, target_is_directory=True)
        return real(*args)
    monkeypatch.setattr(SandboxBuilder, "_shared_text_masks", staticmethod(swapped))
    cmd = _rendered(SandboxBuilder(cfg), ["codex"])
    assert sorted(p.name for p in out.iterdir()) == ["AGENTS.override.md"]
    assert (out / "AGENTS.override.md").is_dir()
    masks = [cmd[i + 2] for i in range(len(cmd) - 2) if cmd[i] == "--ro-bind" and cmd[i + 1] == os.devnull]
    assert f"/users/alice/{_CODEX}/AGENTS.md" in masks
