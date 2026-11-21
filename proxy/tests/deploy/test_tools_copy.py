"""``scripts/copy-tools-to-compute.sh``: the community tools' one hand-over
from the server's folder to the compute machine's, at the update that
splits them."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"

TOOLS_COPY = SCRIPTS / "copy-tools-to-compute.sh"
TOOLS_MARK = ".tools-copied"
_INTERPRETER = "cpython-3.13.1-linux-x86_64-gnu"


def _tool(root: Path, name: str, runtime: str = "python", *, compact: bool = False, **files: str) -> Path:
    tool = root / name
    tool.mkdir(parents=True)
    manifest = {"name": name, "server": {"runtime": runtime, "command": "run"}}
    (tool / "manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":")) if compact else json.dumps(manifest, indent=2))
    for rel, text in files.items():
        (tool / rel).parent.mkdir(parents=True, exist_ok=True)
        (tool / rel).write_text(text)
    return tool


def _servers_tools(tmp_path: Path) -> Path:
    """The server's community folder as an update finds it: two built tools,
    their interpreter with its minor-version link, and everything the copy
    leaves out."""
    src = tmp_path / "server-tools"
    alpha = _tool(src, "alpha", **{"server.py": "print('a')\n", "keys/token": "secret\n",
                                   "venv/pyvenv.cfg": f"home = {src}/.uv-python/{_INTERPRETER}/bin\n"})
    (alpha / "keys").chmod(0o700)
    (alpha / "keys" / "token").chmod(0o600)
    (alpha / "venv" / "bin").mkdir()
    (alpha / "venv" / "bin" / "python").symlink_to(f"../../../.uv-python/{_INTERPRETER}/bin/python3.13")
    _tool(src, "beta", "node", **{"node_modules/pkg/index.js": "module.exports = 1\n"})
    python = src / ".uv-python" / _INTERPRETER / "bin" / "python3.13"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    (src / ".uv-python" / "cpython-3.13-linux-x86_64-gnu").symlink_to(_INTERPRETER)
    # Left out: an update's backup, a failed install, a staged folder, hidden
    # entries, a folder that is no tool, a file and a link.
    for name in ("alpha.bak", "gamma.failed", "delta.new", ".quarantine-alpha-1a2b3c4d", ".venv-aside-x"):
        _tool(src, name)
    (src / ".install-in-progress-alpha").write_text("pending")
    (src / "stray").mkdir()
    (src / "notes.txt").write_text("x")
    (src / "linked").symlink_to("alpha")
    return src


def _copy_tools(src: Path, dst: Path, **env: str) -> subprocess.CompletedProcess:
    dst.mkdir(exist_ok=True)
    return subprocess.run(["sh", str(TOOLS_COPY), str(src), str(dst)], capture_output=True, text=True,
                          timeout=60, check=False,
                          env={"PATH": os.environ["PATH"], "LC_ALL": "C", **env})


def test_the_tools_copy_brings_every_tool_and_interpreter_and_marks_the_compute_folder(tmp_path):
    src, dst = _servers_tools(tmp_path), tmp_path / "compute-tools"
    r = _copy_tools(src, dst)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "copy-tools-to-compute: Copied 2 community tools to the compute machine.\n"
    assert sorted(p.name for p in dst.iterdir()) == [TOOLS_MARK, ".uv-python", "alpha", "beta"]
    assert sorted(p.name for p in (dst / ".uv-python").iterdir()) == ["cpython-3.13-linux-x86_64-gnu", _INTERPRETER]
    # Mode and links kept: a tool's keys stay its own, its venv still names
    # its interpreter by the link it was built with.
    assert stat.S_IMODE((dst / "alpha" / "keys").stat().st_mode) == 0o700
    assert stat.S_IMODE((dst / "alpha" / "keys" / "token").stat().st_mode) == 0o600
    assert (dst / "alpha" / "keys" / "token").read_text() == "secret\n"
    assert os.readlink(dst / "alpha" / "venv" / "bin" / "python") == (
        f"../../../.uv-python/{_INTERPRETER}/bin/python3.13")
    assert os.readlink(dst / ".uv-python" / "cpython-3.13-linux-x86_64-gnu") == _INTERPRETER
    assert stat.S_IMODE((dst / ".uv-python" / _INTERPRETER / "bin" / "python3.13").stat().st_mode) == 0o755
    assert (dst / "beta" / "node_modules" / "pkg" / "index.js").is_file()
    assert (dst / TOOLS_MARK).read_text().startswith("copied 20")
    assert not (src / TOOLS_MARK).exists()
    assert not any(p.name.startswith(".copying-") for p in (*dst.iterdir(), *(dst / ".uv-python").iterdir()))


# The file a Docker tool keeps its settings and its admin-set secrets in.
_SETTINGS = "." + "env"


def test_the_tools_copy_takes_programs_only(tmp_path):
    """The compute machine builds and runs Python and Node programs. A
    Docker tool is the server's to run: its folder holds a compose file and
    the settings file with the secrets an admin set, and none of it goes to
    the compute machine, which holds no secret of the platform. A program is
    copied with or without an environment: one with none can still hold the
    keys a tool wrote, which no later install brings."""
    src, dst = tmp_path / "server-tools", tmp_path / "compute-tools"
    _tool(src, "crm", "docker", **{"docker-compose.yml": "services: {}\n", _SETTINGS: "CRM_API_KEY=s3cr3t\n"})
    _tool(src, "crm-compact", "docker", compact=True, **{_SETTINGS: "CRM_API_KEY=s3cr3t\n"})
    _tool(src, "hosted", "none", **{"keys/token": "s3cr3t\n"})
    # A program's manifest beside a compose file is still a Docker tool's folder.
    _tool(src, "wrapped", "python", **{"docker-compose.yml": "services: {}\n", _SETTINGS: "KEY=s3cr3t\n"})
    _tool(src, "built", "python", **{"venv/pyvenv.cfg": "home = /x\n"})
    _tool(src, "bare", "node", compact=True, **{"keys/token": "kept\n"})
    _tool(src, "spelled", " Python ", **{"config/own.json": "{}\n"})
    (src / "unread").mkdir()
    (src / "unread" / "manifest.json").write_text("not json, and no runtime")
    r = _copy_tools(src, dst)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "copy-tools-to-compute: Copied 3 community tools to the compute machine.\n"
    assert sorted(p.name for p in dst.iterdir()) == [TOOLS_MARK, "bare", "built", "spelled"]
    assert (dst / "bare" / "keys" / "token").read_text() == "kept\n"
    held = [p for p in dst.rglob("*") if p.is_file()]
    assert not [p for p in held if p.name == _SETTINGS or b"s3cr3t" in p.read_bytes()]
    # The server keeps every folder it had.
    assert (src / "crm" / _SETTINGS).read_text() == "CRM_API_KEY=s3cr3t\n"


def test_the_tools_copy_is_the_one_hand_over(tmp_path):
    """With its mark the compute machine's folder is never touched again:
    what it holds is the live copy."""
    src, dst = _servers_tools(tmp_path), tmp_path / "compute-tools"
    assert _copy_tools(src, dst).returncode == 0
    _tool(src, "late")
    (dst / "alpha" / "server.py").write_text("the machine's own build\n")
    again = _copy_tools(src, dst)
    assert (again.returncode, again.stdout, again.stderr) == (0, "", "")
    assert not (dst / "late").exists()
    assert (dst / "alpha" / "server.py").read_text() == "the machine's own build\n"


def test_a_compute_volume_that_was_removed_is_filled_again_from_the_servers(tmp_path):
    """The compute volume lost while the server's stays (an admin removed
    it): the server's folder is copied again. A tool the compute machine
    built since the hand-over has no environment on the server, so it
    arrives without one and the compute machine installs it again. A new
    host is another case: no backup holds either volume."""
    src = _servers_tools(tmp_path)
    assert _copy_tools(src, tmp_path / "compute-tools").returncode == 0
    _tool(src, "late", **{"server.py": "print('built on the machine')\n"})
    fresh = tmp_path / "new-volume"
    r = _copy_tools(src, fresh)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "copy-tools-to-compute: Copied 3 community tools to the compute machine.\n"
    assert sorted(p.name for p in fresh.iterdir()) == [TOOLS_MARK, ".uv-python", "alpha", "beta", "late"]
    assert not (fresh / "late" / "venv").exists() and not (fresh / "late" / "node_modules").exists()


def test_a_tools_copy_that_stopped_is_done_again_and_keeps_what_the_machine_built(tmp_path):
    """A run that stopped left a tool under its staging name and no mark. The
    next start drops the staged folder, copies what is missing and never
    replaces a tool the destination holds: the compute machine's is the live one."""
    src, dst = _servers_tools(tmp_path), tmp_path / "compute-tools"
    _tool(dst, ".copying-alpha", **{"half": "x"})
    (dst / ".uv-python" / f".copying-{_INTERPRETER}").mkdir(parents=True)
    _tool(dst, "beta", **{"built-here": "the machine's own build\n"})
    r = _copy_tools(src, dst)
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "copy-tools-to-compute: Copied 1 community tool to the compute machine.\n"
    assert sorted(p.name for p in dst.iterdir()) == [TOOLS_MARK, ".uv-python", "alpha", "beta"]
    assert (dst / "alpha" / "server.py").is_file() and not (dst / "alpha" / "half").exists()
    assert (dst / "beta" / "built-here").is_file() and not (dst / "beta" / "node_modules").exists()
    assert sorted(p.name for p in (dst / ".uv-python").iterdir()) == ["cpython-3.13-linux-x86_64-gnu", _INTERPRETER]


def test_a_tools_copy_with_too_little_free_space_copies_nothing_and_says_so(tmp_path):
    src, dst = _servers_tools(tmp_path), tmp_path / "compute-tools"
    r = _copy_tools(src, dst, OTODOCK_TOOLS_COPY_TEST_FREE_KIB="1")
    assert r.returncode == 3
    said = r.stdout.strip()
    assert re.fullmatch(
        r"copy-tools-to-compute: The community tools were not copied to the compute machine\. They need "
        r"\d+ MB and its tools volume has 0 MB free\. OtoDock installs each tool again there, which takes a "
        r"while\. To copy them instead, free disk space and start OtoDock again\.", said), said
    assert list(dst.iterdir()) == [] and not (src / TOOLS_MARK).exists()
    # The space is there at a later start: the copy is made then.
    assert _copy_tools(src, dst, OTODOCK_TOOLS_COPY_TEST_FREE_KIB="10000000").returncode == 0
    assert (dst / "alpha").is_dir() and (dst / TOOLS_MARK).is_file()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file with no permission bits")
def test_a_tool_that_cannot_be_copied_is_named_and_copied_at_a_later_start(tmp_path):
    src, dst = _servers_tools(tmp_path), tmp_path / "compute-tools"
    (src / "alpha" / "keys" / "token").chmod(0)
    r = _copy_tools(src, dst)
    assert r.returncode == 1
    assert ("copy-tools-to-compute: These could not be copied to the compute machine: alpha. OtoDock "
            "installs them again there, or the next start copies them, whichever comes first.") in r.stdout
    assert sorted(p.name for p in dst.iterdir()) == [".uv-python", "beta"]
    assert not (src / TOOLS_MARK).exists()
    (src / "alpha" / "keys" / "token").chmod(0o600)
    later = _copy_tools(src, dst)
    assert later.returncode == 0, later.stdout + later.stderr
    assert (dst / "alpha" / "keys" / "token").read_text() == "secret\n" and (dst / TOOLS_MARK).is_file()


def test_the_tools_copy_of_a_fresh_install_copies_nothing_and_marks_the_compute_folder(tmp_path):
    src, dst = tmp_path / "server-tools", tmp_path / "compute-tools"
    src.mkdir()
    r = _copy_tools(src, dst)
    assert (r.returncode, r.stdout) == (0, "")
    assert [p.name for p in dst.iterdir()] == [TOOLS_MARK] and list(src.iterdir()) == []


def test_the_tools_copy_refuses_a_call_it_cannot_run(tmp_path):
    one = subprocess.run(["sh", str(TOOLS_COPY), str(tmp_path)], capture_output=True, text=True, check=False)
    assert one.returncode == 2 and "usage: copy-tools-to-compute.sh" in one.stderr
    gone = subprocess.run(["sh", str(TOOLS_COPY), str(tmp_path / "missing"), str(tmp_path)],
                          capture_output=True, text=True, check=False)
    assert gone.returncode == 2 and "a tools folder is missing" in gone.stderr
    assert list(tmp_path.iterdir()) == []


def test_the_mark_is_written_through_a_new_file_of_its_own(tmp_path):
    """The compute machine writes its tools folder: a link it left at the
    mark's former temporary name is never written through, and the mark
    replaces whatever stood at its own name."""
    src, dst = _servers_tools(tmp_path), tmp_path / "compute-tools"
    dst.mkdir()
    kept = tmp_path / "kept.txt"
    kept.write_text("not the copy's\n")
    (dst / f"{TOOLS_MARK}.tmp").symlink_to(kept)
    r = _copy_tools(src, dst)
    assert r.returncode == 0, r.stdout + r.stderr
    assert kept.read_text() == "not the copy's\n"
    mark = dst / TOOLS_MARK
    assert mark.is_file() and not mark.is_symlink() and mark.read_text().startswith("copied ")
    assert not [p.name for p in dst.iterdir() if p.name.startswith(f"{TOOLS_MARK}.") and p.name != f"{TOOLS_MARK}.tmp"]


def test_a_link_left_at_a_tools_name_during_the_copy_is_never_moved_into(tmp_path):
    """The compute machine may leave a link at a tool's name while the copy
    runs (a stand-in cp leaves one, to a folder of this test's own, right
    after it copied): the staged folder is never moved into the folder the
    link names. That tool is not copied (named, no mark), the rest are."""
    src, dst = _servers_tools(tmp_path), tmp_path / "compute-tools"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    shim = tmp_path / "shim"
    shim.mkdir()
    real_cp = shutil.which("cp")
    (shim / "cp").write_text(f'#!/bin/sh\n{real_cp} "$@" || exit 1\n'
                             f'for last; do :; done\n'
                             f'case "$last" in */.copying-alpha) ln -s {elsewhere} "{dst}/alpha" ;; esac\n')
    (shim / "cp").chmod(0o755)
    r = _copy_tools(src, dst, PATH=f"{shim}:{os.environ['PATH']}")
    assert r.returncode == 1, r.stdout + r.stderr
    assert "could not be copied to the compute machine: alpha." in r.stdout
    assert list(elsewhere.iterdir()) == []
    assert (dst / "alpha").is_symlink() and not (dst / ".copying-alpha").exists()
    assert (dst / "beta" / "manifest.json").is_file() and not (dst / TOOLS_MARK).exists()
