"""Tests for services.mcp.mcp_output_relocation — post-tool file moves.

The dest is now a FLAT, hidden ``.screenshots`` (no per-session subdir). Files are
moved precisely by the filename(s) named in the tool result (move-by-filename),
falling back to an ``mtime > tool_start`` scan when no result text is available.
``keep_recent`` caps the dest dir; ``gc_after`` is gone for camoufox.
"""

import time
from pathlib import Path
from unittest.mock import MagicMock, patch


def _fake_manifest(mcp_dir: Path, screenshots_dir: Path, *, keep_recent=None, gc_after=None):
    from services.mcp.mcp_registry import OutputRelocationDef
    m = MagicMock()
    m.name = "fake-mcp"
    m.server_name = "fake"
    m.mcp_dir = mcp_dir
    m.outputs = [OutputRelocationDef(
        source=str(screenshots_dir),
        destination_template="${workspace_dir}/.screenshots",
        after_tools=["*"],
        keep_recent=keep_recent,
        gc_after=gc_after,
    )]
    return m


def _dest(agents_dir: Path, agent="agent-x", user="alice") -> Path:
    return agents_dir / agent / "users" / user / "workspace" / ".screenshots"


def test_move_by_filename_is_precise(tmp_path: Path, temp_db, monkeypatch):
    """With result text, ONLY the named file moves — not a concurrent session's
    newer file sitting in the same shared source (no cross-user grab)."""
    import config as app_config
    from services.mcp import mcp_output_relocation

    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"; source.mkdir()
    (source / "page-mine.png").write_bytes(b"MINE")
    (source / "page-someone-else.png").write_bytes(b"OTHER")  # another session's, also new

    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    fake_ctx = MagicMock(agent="agent-x", username="alice", mount_username="alice")

    with patch("services.mcp.mcp_registry.get_manifest", return_value=_fake_manifest(mcp_dir, source)), \
         patch("core.session.session_state.get_session_security", return_value=fake_ctx):
        moved = mcp_output_relocation.relocate_for_tool(
            "sess-1", "fake-mcp", "browser_take_screenshot",
            result_text="### Result\n- [Screenshot of viewport](screenshots/page-mine.png)\n",
        )
    assert [p.name for p in moved] == ["page-mine.png"]
    assert (_dest(agents_dir) / "page-mine.png").read_bytes() == b"MINE"
    # The other session's file was NOT grabbed.
    assert (source / "page-someone-else.png").exists()
    assert not (_dest(agents_dir) / "page-someone-else.png").exists()
    # FLAT — no per-session subdir.
    assert not (_dest(agents_dir) / "sess-1").exists()


def test_mtime_fallback_when_no_result_text(tmp_path: Path, temp_db, monkeypatch):
    """No result text (Codex/interactive) → move files newer than tool-start."""
    import config as app_config
    from services.mcp import mcp_output_relocation

    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"; source.mkdir()
    (source / "old.png").write_bytes(b"OLD")

    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    fake_ctx = MagicMock(agent="agent-x", username="alice", mount_username="alice")

    with patch("services.mcp.mcp_registry.get_manifest", return_value=_fake_manifest(mcp_dir, source)), \
         patch("core.session.session_state.get_session_security", return_value=fake_ctx):
        mcp_output_relocation.record_tool_start("sess-1", "fake-mcp")
        time.sleep(0.01)
        (source / "new.png").write_bytes(b"NEW")
        moved = mcp_output_relocation.relocate_for_tool("sess-1", "fake-mcp", "anytool")
    assert [p.name for p in moved] == ["new.png"]
    assert (_dest(agents_dir) / "new.png").read_bytes() == b"NEW"
    assert (source / "old.png").exists()  # predates tool-start → left


def test_parse_rejects_path_traversal(tmp_path: Path, temp_db, monkeypatch):
    """A result naming a traversal path must never move anything outside source."""
    import config as app_config
    from services.mcp import mcp_output_relocation

    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"; source.mkdir()
    secret = tmp_path / "secret.png"; secret.write_bytes(b"SECRET")

    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    fake_ctx = MagicMock(agent="agent-x", username="alice", mount_username="alice")

    with patch("services.mcp.mcp_registry.get_manifest", return_value=_fake_manifest(mcp_dir, source)), \
         patch("core.session.session_state.get_session_security", return_value=fake_ctx):
        moved = mcp_output_relocation.relocate_for_tool(
            "sess-1", "fake-mcp", "browser_take_screenshot",
            result_text="see screenshots/../../secret.png",
        )
    # The regex captured only the basename `secret.png`, which isn't in source → no move.
    assert moved == []
    assert secret.read_bytes() == b"SECRET"  # untouched


def test_keep_recent_caps_dest(tmp_path: Path, temp_db, monkeypatch):
    """keep_recent trims the dest dir to the N newest files."""
    import config as app_config
    from services.mcp import mcp_output_relocation

    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"; source.mkdir()
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    fake_ctx = MagicMock(agent="agent-x", username="alice", mount_username="alice")
    manifest = _fake_manifest(mcp_dir, source, keep_recent=3)

    with patch("services.mcp.mcp_registry.get_manifest", return_value=manifest), \
         patch("core.session.session_state.get_session_security", return_value=fake_ctx):
        for i in range(5):
            (source / f"page-{i}.png").write_bytes(bytes([i]))
            mcp_output_relocation.relocate_for_tool(
                "sess-1", "fake-mcp", "browser_take_screenshot",
                result_text=f"(screenshots/page-{i}.png)",
            )
            time.sleep(0.01)  # distinct mtimes
    remaining = sorted(p.name for p in _dest(agents_dir).iterdir())
    assert remaining == ["page-2.png", "page-3.png", "page-4.png"]  # newest 3


def test_cleanup_session_noop_without_gc_after(tmp_path: Path, temp_db, monkeypatch):
    """With keep_recent (no gc_after), cleanup_session must NOT delete the flat dir."""
    import config as app_config
    from services.mcp import mcp_output_relocation

    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"; source.mkdir()
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    fake_ctx = MagicMock(agent="agent-x", username="alice", mount_username="alice")
    manifest = _fake_manifest(mcp_dir, source, keep_recent=15)

    with patch("services.mcp.mcp_registry.get_manifest", return_value=manifest), \
         patch("services.mcp.mcp_registry.get_all_manifests", return_value={"fake-mcp": manifest}), \
         patch("core.session.session_state.get_session_security", return_value=fake_ctx):
        (source / "page-x.png").write_bytes(b"X")
        mcp_output_relocation.relocate_for_tool(
            "sess-1", "fake-mcp", "browser_take_screenshot",
            result_text="(screenshots/page-x.png)",
        )
        assert (_dest(agents_dir) / "page-x.png").exists()
        mcp_output_relocation.cleanup_session("sess-1", "agent-x", "alice")
        # Data-loss guard: the flat dest + its file survive (no gc_after).
        assert (_dest(agents_dir) / "page-x.png").exists()


def test_t2_uses_docker_cp(tmp_path: Path, temp_db, monkeypatch):
    """In T2 the source is an invisible named volume → pull via `docker cp`."""
    import config as app_config
    from core.config import deployment
    from services.mcp import mcp_output_relocation

    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"  # NOT created — proxy can't see the volume
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    monkeypatch.setattr(app_config, "INSTALL_ID", "testinst")
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    monkeypatch.setattr(deployment, "docker_subprocess_env", lambda: {})
    fake_ctx = MagicMock(agent="agent-x", username="alice", mount_username="alice")

    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        dst = Path(cmd[-1])             # docker cp <c>:<src> <dst>
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"PNG")
        return MagicMock(returncode=0, stderr="")

    with patch("services.mcp.mcp_registry.get_manifest", return_value=_fake_manifest(mcp_dir, source)), \
         patch("core.session.session_state.get_session_security", return_value=fake_ctx), \
         patch.object(mcp_output_relocation.subprocess, "run", side_effect=fake_run):
        moved = mcp_output_relocation.relocate_for_tool(
            "sess-1", "fake-mcp", "browser_take_screenshot",
            result_text="(screenshots/page-x.png)",
        )
    assert [p.name for p in moved] == ["page-x.png"]
    assert (_dest(agents_dir) / "page-x.png").read_bytes() == b"PNG"
    # docker cp targeted the namespaced container + the in-container /screenshots path.
    assert calls and calls[0][:2] == ["docker", "cp"]
    assert calls[0][2] == "otodock-testinst-mcp-fake-mcp:/screenshots/page-x.png"


def test_no_outputs_is_noop(tmp_path: Path, temp_db, monkeypatch):
    from services.mcp import mcp_output_relocation
    m = MagicMock(); m.outputs = []
    with patch("services.mcp.mcp_registry.get_manifest", return_value=m):
        mcp_output_relocation.record_tool_start("sess-1", "plain-mcp")
        assert mcp_output_relocation.relocate_for_tool("sess-1", "plain-mcp", "x") == []


def test_a_shared_only_chat_relocates_into_the_shared_workspace(tmp_path: Path, monkeypatch):
    """The MOUNT decides the workspace: a Shared-only human chat (a username,
    an agent-scope mount) works in the agent's ``workspace`` and its
    screenshots land there, never in a user folder it does not mount."""
    import config as app_config
    from auth.path_policy import SecurityContext
    from services.mcp import mcp_output_relocation

    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"; source.mkdir()
    (source / "page-1.png").write_bytes(b"PNG")
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    ctx = SecurityContext(role="editor", username="alice", agent="agent-x",
                          is_admin_agent=False, session_scope="agent")

    with patch("services.mcp.mcp_registry.get_manifest", return_value=_fake_manifest(mcp_dir, source)), \
         patch("core.session.session_state.get_session_security", return_value=ctx):
        moved = mcp_output_relocation.relocate_for_tool(
            "sess-so", "fake-mcp", "browser_take_screenshot", result_text="page-1.png")
    assert moved == [agents_dir / "agent-x" / "workspace" / ".screenshots" / "page-1.png"]
    assert not (agents_dir / "agent-x" / "users").exists()
    assert mcp_output_relocation.session_workspace_shown(ctx) == "/workspace"
