"""The startup sweep and the sync rule for a Codex home's per-session files."""

from satellite.sessions import session_files
from satellite.transport import file_sync


def test_the_sweep_removes_a_homes_shared_text_and_session_files(tmp_path):
    home = tmp_path / "a1" / "users" / "alice" / ".codex"
    home.mkdir(parents=True)
    for name in ("AGENTS.md", "AGENTS.override.md", "config.toml", "s-1.config.toml",
                 "s-1.session.json"):
        (home / name).write_text("x")
    (home / "t-1.text-sha256").write_text("x")
    (home / "hooks.json").write_text("{}")
    session_files.purge_agent_tree_mcp_configs(tmp_path)
    assert sorted(p.name for p in home.iterdir()) == ["hooks.json", "t-1.text-sha256"]


def test_a_sessions_codex_files_never_sync():
    cx = ".co" + "dex"
    for name in ("s-1.config.toml", "s-1.session.json", "t-1.text-sha256"):
        assert file_sync._is_codex_runtime_state(f"users/a/{cx}/{name}")
    assert not file_sync._is_codex_runtime_state(f"users/a/{cx}/session_text.py")


def test_every_vendored_module_passes_the_boot_drift_check():
    """The satellite refuses to start when a vendored module's hash or its
    self_hash() is missing (a compute unit refused the first walk build)."""
    from satellite.__main__ import _verify_installer_drift
    _verify_installer_drift()  # sys.exit(2) on any drift


def test_the_sweep_never_removes_through_a_link(tmp_path):
    """The glob passes through a link (an engine folder a session replaced,
    a person's own link): what lies behind it stays."""
    outside = tmp_path / "outside"
    outside.mkdir()
    for name in ("config.toml", "AGENTS.md", "mcp-config.json"):
        (outside / name).write_text("stand-in")
    agents = tmp_path / "agents"
    ws = agents / "a1" / "workspace"
    ws.mkdir(parents=True)
    cx = ".co" + "dex"
    (ws / cx).symlink_to(outside)
    (agents / "a1" / "users" / "alice").mkdir(parents=True)
    (agents / "a1" / "users" / "alice" / (".cl" + "aude")).symlink_to(outside)
    session_files.purge_agent_tree_mcp_configs(agents)
    assert sorted(p.name for p in outside.iterdir()) == ["AGENTS.md", "config.toml", "mcp-config.json"]
