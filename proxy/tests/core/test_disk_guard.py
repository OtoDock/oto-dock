"""The disk-write guard itself (``tests/_disk_guard.py``, fixture
``loop_disk_guard``): it fails on a write made on the guarded thread, even one
the code under test swallows, and lets every other thread write.

Run: cd proxy && venv/bin/pytest tests/core/test_disk_guard.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import shutil
import tarfile
import threading
from pathlib import Path

import pytest

from tests._disk_guard import LoopDiskViolation


def test_a_write_on_the_guarded_thread_fails(loop_disk_guard, tmp_path):
    with pytest.raises(LoopDiskViolation):
        with loop_disk_guard.active():
            with open(tmp_path / "x", "w") as f:
                f.write("x")
    assert not (tmp_path / "x").exists()


def test_a_swallowed_write_still_fails_at_the_block_s_end(loop_disk_guard, tmp_path):
    def writer_that_logs_and_goes_on():
        with contextlib.suppress(Exception):
            os.replace(tmp_path / "a", tmp_path / "b")

    (tmp_path / "a").write_text("a")
    with pytest.raises(LoopDiskViolation, match="test_disk_guard.py"):
        with loop_disk_guard.active():
            writer_that_logs_and_goes_on()
    assert loop_disk_guard.sites[0][3] == "replace"


def test_a_read_passes(loop_disk_guard, tmp_path):
    (tmp_path / "r").write_text("r")
    with loop_disk_guard.active():
        assert open(tmp_path / "r").read() == "r"
        assert Path(tmp_path / "r").read_text() == "r"
        fd = os.open(tmp_path / "r", os.O_RDONLY)
        os.close(fd)


def test_another_thread_writes_freely(loop_disk_guard, tmp_path):
    def work():
        (tmp_path / "t").write_text("t")
        os.replace(tmp_path / "t", tmp_path / "u")
        shutil.rmtree(tmp_path / "gone", ignore_errors=True)

    with loop_disk_guard.active():
        t = threading.Thread(target=work)
        t.start()
        t.join()
    assert (tmp_path / "u").read_text() == "t"


def test_asyncio_to_thread_is_the_way_out(loop_disk_guard, tmp_path):
    async def scenario():
        with loop_disk_guard.active():
            await asyncio.to_thread((tmp_path / "w").write_text, "w")

    asyncio.run(scenario())
    assert (tmp_path / "w").read_text() == "w"


def _write_text(p):
    p.write_text("x")


def _write_bytes(p):
    p.write_bytes(b"x")


def _fsync(p):
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _open_for_writing(p):
    os.open(p, os.O_WRONLY | os.O_CREAT, 0o600)


def _rmtree(p):
    shutil.rmtree(p.parent / "nothing", ignore_errors=True)


def _copy(p):
    shutil.copy(__file__, p)


def _unlink(p):
    os.unlink(p)


def _path_unlink(p):
    p.unlink(missing_ok=True)


def _makedirs(p):
    os.makedirs(p.parent / "d", exist_ok=True)


def _chmod(p):
    os.chmod(p.parent, 0o755)


def _touch(p):
    p.touch()


def _child(p):
    import subprocess
    subprocess.run(["true"], check=False)


@pytest.mark.parametrize("call", [_write_text, _write_bytes, _fsync, _open_for_writing, _rmtree,
                                  _copy, _unlink, _path_unlink, _makedirs, _chmod, _touch, _child])
def test_each_write_kind_is_caught(loop_disk_guard, tmp_path, call):
    with pytest.raises(LoopDiskViolation):
        with loop_disk_guard.active(), contextlib.suppress(OSError):
            call(tmp_path / "f")


def test_the_guard_disarms_after_the_block(loop_disk_guard, tmp_path):
    with loop_disk_guard.active():
        pass
    (tmp_path / "after").write_text("ok")
    assert not hasattr(os.replace, "__wrapped__") and not hasattr(open, "__wrapped__")


def _fileio(p):
    io.FileIO(p, "w").close()


def _tar(p):
    tarfile.open(p, "w").close()


def _sync(p):
    os.sync()


def _chown(p):
    os.chown(p.parent, -1, -1)


@pytest.mark.parametrize("call", [_fileio, _tar, _sync, _chown])
def test_the_less_common_spellings_are_caught(loop_disk_guard, tmp_path, call):
    with pytest.raises(LoopDiskViolation):
        with loop_disk_guard.active(), contextlib.suppress(OSError):
            call(tmp_path / "f")


@pytest.mark.parametrize("name", ["pwrite", "writev", "posix_fallocate"])
def test_a_write_through_a_descriptor_opened_before_is_caught(loop_disk_guard, tmp_path, name):
    fd = os.open(tmp_path / "d", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        call = {"pwrite": lambda: os.pwrite(fd, b"x", 0),
                "writev": lambda: os.writev(fd, [b"x"]),
                "posix_fallocate": lambda: os.posix_fallocate(fd, 0, 1)}[name]
        with pytest.raises(LoopDiskViolation):
            with loop_disk_guard.active():
                call()
    finally:
        os.close(fd)


def test_a_file_object_read_through_fileio_passes(loop_disk_guard, tmp_path):
    (tmp_path / "r").write_text("r")
    with loop_disk_guard.active():
        with io.FileIO(tmp_path / "r", "r") as f:
            assert f.read() == b"r"
            assert isinstance(f, io.FileIO)
