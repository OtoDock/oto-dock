"""Tests for services.mcp.mcp_output_relocation — post-tool file moves.

The dest is now a FLAT, hidden ``.screenshots`` (no per-session subdir). Files are
moved precisely by the filename(s) named in the tool result (move-by-filename),
falling back to an ``mtime > tool_start`` scan when no result text is available.
``keep_recent`` caps the dest dir; ``gc_after`` is gone for camoufox.
"""

import io
import os
import shutil
import tarfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _agents_root(tmp_path: Path) -> None:
    """The agents root every test points ``AGENTS_DIR`` at: the safe file
    helpers open it as their root, so it exists, as it does on an install."""
    (tmp_path / "agents").mkdir(exist_ok=True)


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


def test_t2_uses_docker_cp(tmp_path: Path, temp_db, monkeypatch, docker_stand_in):
    """In T2 the source is an invisible named volume → pull via `docker cp`."""
    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"  # NOT created — proxy can't see the volume
    log = docker_stand_in([("page-x.png", "file", b"PNG")])
    moved = _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source),
                      result_text="(screenshots/page-x.png)")
    assert [p.name for p in moved] == ["page-x.png"]
    assert (_dest(tmp_path / "agents") / "page-x.png").read_bytes() == b"PNG"
    # docker cp targeted the namespaced container + the in-container /screenshots path.
    assert log.read_text().splitlines() == ["cp otodock-testinst-mcp-fake-mcp:/screenshots/page-x.png -"]

def _relocate(tmp_path, monkeypatch, manifest, result_text="(screenshots/page-a.png)", *, start=False):
    import config as app_config
    from services.mcp import mcp_output_relocation
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    fake_ctx = MagicMock(agent="agent-x", username="alice", mount_username="alice")
    with patch("services.mcp.mcp_registry.get_manifest", return_value=manifest), \
         patch("core.session.session_state.get_session_security", return_value=fake_ctx):
        if start:
            mcp_output_relocation.record_tool_start("sess-1", "fake-mcp")
        return mcp_output_relocation.relocate_for_tool(
            "sess-1", "fake-mcp", "browser_take_screenshot", result_text=result_text)


def _tool_folder(tmp_path: Path) -> tuple[Path, Path]:
    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"; source.mkdir()
    return mcp_dir, source


def _stand_ins(folder: Path, count: int) -> dict[str, bytes]:
    folder.mkdir(parents=True, exist_ok=True)
    files = {f"keep-{i}.png": f"stand-in {i}".encode() for i in range(count)}
    for name, data in files.items():
        (folder / name).write_bytes(data)
    return files


def test_a_destination_reached_through_a_link_gets_nothing_and_loses_nothing(tmp_path: Path, monkeypatch):
    """The session's ``.screenshots`` replaced by a link: nothing is written
    through it and the trim removes nothing where it points."""
    mcp_dir, source = _tool_folder(tmp_path)
    (source / "page-a.png").write_bytes(b"A")
    elsewhere = tmp_path / "elsewhere"
    files = _stand_ins(elsewhere, 5)
    dest = _dest(tmp_path / "agents")
    dest.parent.mkdir(parents=True)
    dest.symlink_to(elsewhere)
    moved = _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source, keep_recent=1))
    assert moved == []
    assert {p.name: p.read_bytes() for p in elsewhere.iterdir()} == files
    assert (source / "page-a.png").read_bytes() == b"A"


def test_a_link_in_the_tool_folder_is_never_taken(tmp_path: Path, monkeypatch, caplog):
    """An entry of the tool's output folder that is a link moves nowhere,
    and no link lands in the workspace. The listing passes it over: the
    copy is never tried, so the shared folder costs no warning per link."""
    mcp_dir, source = _tool_folder(tmp_path)
    target = tmp_path / "stand-in-secret.png"
    target.write_bytes(b"STAND-IN")
    (source / "page-a.png").symlink_to(target)
    with caplog.at_level("WARNING", logger="otodock.mcp-output-relocation"):
        moved = _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source))
    assert moved == []
    assert caplog.records == []
    dest_file = _dest(tmp_path / "agents") / "page-a.png"
    assert not dest_file.exists() and not dest_file.is_symlink()
    assert target.read_bytes() == b"STAND-IN"


def test_the_trim_counts_and_removes_regular_files_only(tmp_path: Path, monkeypatch):
    """A link in the destination is neither counted nor removed, and what it
    points at stays."""
    mcp_dir, source = _tool_folder(tmp_path)
    dest = _dest(tmp_path / "agents")
    dest.mkdir(parents=True)
    target = tmp_path / "stand-in-target.png"
    target.write_bytes(b"T")
    os.utime(target, (1, 1))
    (dest / "aaa-link.png").symlink_to(target)
    (dest / "old.png").write_bytes(b"O")
    os.utime(dest / "old.png", (100, 100))
    (source / "page-a.png").write_bytes(b"A")
    _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source, keep_recent=1))
    assert sorted(p.name for p in dest.iterdir()) == ["aaa-link.png", "page-a.png"]
    assert target.read_bytes() == b"T"


@pytest.mark.parametrize(("source_t", "dest_t"), [
    ("${mcp_dir}/screenshots", "${mcp_dir}/out"),
    ("${mcp_dir}/screenshots", "${workspace_dir}"),
    ("${mcp_dir}/screenshots", "${workspace_dir}/../elsewhere"),
    ("${workspace_dir}/shots", "${workspace_dir}/.screenshots"),
    ("${mcp_dir}", "${workspace_dir}/.screenshots"),
], ids=["dest-in-the-tool-folder", "dest-is-the-workspace", "dest-climbs-out", "source-in-the-workspace",
        "source-is-the-tool-folder"])
def test_outputs_named_outside_their_folders_are_skipped(tmp_path: Path, monkeypatch, source_t, dest_t):
    """A destination outside (or equal to) the session's workspace, or a
    source outside (or equal to) the tool's own folder: nothing moves."""
    from services.mcp.mcp_registry import OutputRelocationDef
    mcp_dir, source = _tool_folder(tmp_path)
    (source / "page-a.png").write_bytes(b"A")
    ws = tmp_path / "agents" / "agent-x" / "users" / "alice" / "workspace"
    (ws / "shots").mkdir(parents=True)
    (ws / "shots" / "page-a.png").write_bytes(b"W")
    manifest = _fake_manifest(mcp_dir, source)
    manifest.outputs = [OutputRelocationDef(source=source_t, destination_template=dest_t, after_tools=["*"],
                                            keep_recent=1)]
    assert _relocate(tmp_path, monkeypatch, manifest) == []
    assert (source / "page-a.png").read_bytes() == b"A"
    assert (ws / "shots" / "page-a.png").read_bytes() == b"W"
    assert not (mcp_dir / "out").exists()


def test_a_named_file_is_taken_only_when_written_during_the_call(tmp_path: Path, monkeypatch):
    """A name in the result picks a file only in the tool's own form and
    only one written since the call started: an older file in the shared
    folder (another session's) stays where it is."""
    mcp_dir, source = _tool_folder(tmp_path)
    (source / "page-old.png").write_bytes(b"OLD")
    os.utime(source / "page-old.png", (time.time() - 3600,) * 2)
    moved = _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source),
                      result_text="(screenshots/page-old.png)", start=True)
    assert moved == []
    assert (source / "page-old.png").read_bytes() == b"OLD"


def test_a_name_counts_only_in_the_tools_own_form(tmp_path: Path, monkeypatch):
    """With a name in the tool's own form in the result, a bare file name
    elsewhere in the same text (a page's words) picks nothing."""
    mcp_dir, source = _tool_folder(tmp_path)
    (source / "page-a.png").write_bytes(b"A")
    (source / "other.png").write_bytes(b"O")
    moved = _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source),
                      result_text="the page says other.png\n- [Screenshot](screenshots/page-a.png)", start=True)
    assert [p.name for p in moved] == ["page-a.png"]
    assert (source / "other.png").read_bytes() == b"O"


def _archive(members: list[tuple[str, str, bytes]]) -> bytes:
    """A tar in Docker's own layout: ``(name, kind, data)``, kind ``file``,
    ``dir`` or ``link`` (data is the link's target)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            info.mtime = int(time.time())
            if kind == "dir":
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            elif kind == "link":
                info.type = tarfile.SYMTYPE
                info.linkname = data.decode()
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def docker_stand_in(tmp_path: Path, monkeypatch):
    """A stand-in ``docker`` first on PATH: it logs its arguments and writes
    the archive the test chose to its output. Fails closed when the real
    one would run."""
    import config as app_config
    from core.config import deployment
    bin_dir = tmp_path / "bin"; bin_dir.mkdir()
    log = tmp_path / "docker-calls.log"
    archive = tmp_path / "archive.tar"
    script = bin_dir / "docker"
    script.write_text(f'#!/bin/sh\necho "$@" >> {log}\ncat {archive}\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    assert shutil.which("docker") == str(script)
    monkeypatch.setattr(app_config, "INSTALL_ID", "testinst")
    monkeypatch.setattr(deployment, "current_mode", lambda: deployment.MANAGED_SOCKPROX)
    monkeypatch.setattr(deployment, "docker_subprocess_env", lambda: {})

    def use(members):
        archive.write_bytes(_archive(members))
        return log
    return use


def test_the_container_copy_takes_regular_files_of_its_own_name_only(tmp_path: Path, monkeypatch, docker_stand_in):
    mcp_dir, source = _tool_folder(tmp_path)
    log = docker_stand_in([("page-x.png", "file", b"PNG"), ("other.png", "file", b"NO"),
                           ("page-y.png", "link", b"/stand-in/target")])
    moved = _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source),
                      result_text="(screenshots/page-x.png) (screenshots/page-y.png)")
    dest = _dest(tmp_path / "agents")
    assert [p.name for p in moved] == ["page-x.png"]
    assert (dest / "page-x.png").read_bytes() == b"PNG"
    assert not (dest / "page-x.png").is_symlink()
    assert sorted(p.name for p in dest.iterdir()) == ["page-x.png"]
    assert log.read_text().splitlines()[0] == "cp otodock-testinst-mcp-fake-mcp:/screenshots/page-x.png -"


def test_the_container_folder_copy_takes_first_level_regular_files_only(tmp_path: Path, monkeypatch,
                                                                       docker_stand_in):
    mcp_dir, source = _tool_folder(tmp_path)
    log = docker_stand_in([("./", "dir", b""), ("./page-a.png", "file", b"A"), ("./sub/", "dir", b""),
                           ("./sub/deep.png", "file", b"D"), ("./page-l.png", "link", b"/stand-in/target"),
                           ("../page-up.png", "file", b"U"), ("./..", "file", b"P")])
    moved = _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source), result_text=None)
    dest = _dest(tmp_path / "agents")
    assert [p.name for p in moved] == ["page-a.png"]
    assert sorted(p.name for p in dest.iterdir()) == ["page-a.png"]
    assert not (tmp_path / "agents" / "agent-x" / "users" / "alice" / "page-up.png").exists()
    assert log.read_text().splitlines()[0] == "cp otodock-testinst-mcp-fake-mcp:/screenshots/. -"


def test_the_container_copy_writes_nothing_through_a_linked_destination(tmp_path: Path, monkeypatch,
                                                                        docker_stand_in):
    """The session's ``.screenshots`` replaced by a link: the container's
    file is not written through it, and the folder it points at keeps its
    files."""
    mcp_dir, source = _tool_folder(tmp_path)
    docker_stand_in([("page-x.png", "file", b"PNG")])
    elsewhere = tmp_path / "elsewhere"
    files = _stand_ins(elsewhere, 5)
    dest = _dest(tmp_path / "agents")
    dest.parent.mkdir(parents=True)
    dest.symlink_to(elsewhere)
    assert _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source),
                     result_text="(screenshots/page-x.png)") == []
    assert {p.name: p.read_bytes() for p in elsewhere.iterdir()} == files


def test_cleanup_skips_a_folder_outside_the_sessions_workspace(tmp_path: Path, monkeypatch):
    """A per-session folder named outside the session's workspace (another
    agent's folder here) is left as it is, as the move skips it."""
    import config as app_config
    from services.mcp import mcp_output_relocation
    from services.mcp.mcp_registry import OutputRelocationDef
    mcp_dir, source = _tool_folder(tmp_path)
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    other = agents_dir / "agent-y" / "workspace" / "cache"
    files = _stand_ins(other / "sess-1", 3)
    manifest = _fake_manifest(mcp_dir, source)
    manifest.outputs = [OutputRelocationDef(source=str(source), destination_template=str(other),
                                            after_tools=["*"], gc_after="session_close")]
    with patch("services.mcp.mcp_registry.get_all_manifests", return_value={"fake-mcp": manifest}):
        mcp_output_relocation.cleanup_session("sess-1", "agent-x", "alice")
    assert {p.name: p.read_bytes() for p in (other / "sess-1").iterdir()} == files


def test_the_container_copy_takes_nothing_past_its_byte_cap(tmp_path: Path, monkeypatch, docker_stand_in):
    from services.mcp import mcp_output_relocation
    monkeypatch.setattr(mcp_output_relocation, "_MAX_OUTPUT_BYTES", 4096)
    mcp_dir, source = _tool_folder(tmp_path)
    docker_stand_in([("page-x.png", "file", b"x" * 20000)])
    assert _relocate(tmp_path, monkeypatch, _fake_manifest(mcp_dir, source),
                     result_text="(screenshots/page-x.png)") == []
    assert not _dest(tmp_path / "agents").exists() or not any(_dest(tmp_path / "agents").iterdir())


def test_cleanup_never_removes_through_a_linked_folder(tmp_path: Path, monkeypatch):
    """The per-session folder sits under a destination that is a link: the
    folder it points at keeps its files."""
    import config as app_config
    from services.mcp import mcp_output_relocation
    mcp_dir, source = _tool_folder(tmp_path)
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    elsewhere = tmp_path / "elsewhere"
    files = _stand_ins(elsewhere / "sess-1", 3)
    dest = _dest(agents_dir)
    dest.parent.mkdir(parents=True)
    dest.symlink_to(elsewhere)
    manifest = _fake_manifest(mcp_dir, source, gc_after="session_close")
    with patch("services.mcp.mcp_registry.get_all_manifests", return_value={"fake-mcp": manifest}):
        mcp_output_relocation.cleanup_session("sess-1", "agent-x", "alice")
    assert {p.name: p.read_bytes() for p in (elsewhere / "sess-1").iterdir()} == files


def test_the_push_to_a_machine_reads_the_moved_file_and_nothing_it_became(tmp_path: Path, monkeypatch):
    """A moved file swapped for a link before the push: the push sends
    nothing, and no other person's file is read."""
    import asyncio

    import config as app_config
    from core.remote import remote_file_flow
    from services.mcp import mcp_output_relocation
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    other = agents_dir / "agent-x" / "users" / "bob" / "workspace" / "stand-in.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"BOB")
    dest = _dest(agents_dir)
    dest.mkdir(parents=True)
    (dest / "page-a.png").symlink_to(other)
    (dest / "page-b.png").write_bytes(b"MINE")
    pushed: list[tuple[str, bytes]] = []

    class _Machines:
        async def push_file(self, machine_id, ref, path, agent_slug=""):
            pushed.append((ref.value, await asyncio.to_thread(Path(path).read_bytes)))
            return True

    info = MagicMock(machine_id="m-1", agent_name="agent-x", files_synced=True)
    monkeypatch.setattr(remote_file_flow, "is_remote_session", lambda sid: True)
    monkeypatch.setattr(remote_file_flow, "_get_remote_session_info", lambda sid: info)
    with patch("core.remote.satellite_connection.get_connection_manager", return_value=_Machines()):
        asyncio.run(mcp_output_relocation.push_relocated_to_satellite(
            "sess-1", [dest / "page-a.png", dest / "page-b.png"]))
    assert pushed == [("users/alice/workspace/.screenshots/page-b.png", b"MINE")]


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


def test_the_move_after_a_tool_call_leaves_the_loop(tmp_path: Path, temp_db, monkeypatch, loop_disk_guard):
    """The pump awaits the move per tool call: the rename (and, on a container
    install, ``docker cp`` for up to 60 s) runs on the file-commit pool."""
    import asyncio

    import config as app_config
    from services.mcp import mcp_output_relocation

    mcp_dir = tmp_path / "mcp"; mcp_dir.mkdir()
    source = mcp_dir / "screenshots"; source.mkdir()
    (source / "page-a.png").write_bytes(b"A")
    agents_dir = tmp_path / "agents"
    monkeypatch.setattr(app_config, "AGENTS_DIR", agents_dir)
    fake_ctx = MagicMock(agent="agent-x", username="alice", mount_username="alice")

    async def scenario():
        with loop_disk_guard.active():
            return await mcp_output_relocation.relocate_and_push_for_tool(
                "sess-1", "fake-mcp", "browser_take_screenshot",
                result_text="(screenshots/page-a.png)")

    with patch("services.mcp.mcp_registry.get_manifest", return_value=_fake_manifest(mcp_dir, source)), \
         patch("core.session.session_state.get_session_security", return_value=fake_ctx):
        moved = asyncio.run(scenario())
    assert [p.name for p in moved] == ["page-a.png"]
    assert (_dest(agents_dir) / "page-a.png").read_bytes() == b"A"
