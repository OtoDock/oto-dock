"""restore.sh's ownership pass, run as root in a user namespace of the
test's own: it owns the restored tree by descriptor and never through a link
a running container put there. The program is the script's own text."""

import json
import os
import subprocess
import sys

import pytest

from tests._paths import REPO_ROOT

SCRIPTS = REPO_ROOT / "scripts"
_CHOWN_SPY = r'''
import json as _json
import os as _os
_log = _os.environ["CHOWN_LOG"]
_swap = _json.loads(_os.environ["CHOWN_SWAP"])
_real_chown, _real_lchown, _armed, _done = _os.chown, _os.lchown, [], []


def _before(name):
    # Armed once the ownership walk owns its first folder ("a": the
    # extraction itself owns only the files), then the folder "b" is
    # replaced by a link just before its file is reached.
    base = _os.path.basename(str(name))
    if base == "a":
        _armed.append(1)
    if base == "f" and _armed and not _done:
        _done.append(1)
        _os.rename(_swap["dir"], _swap["dir"] + ".moved")
        _os.symlink(_swap["to"], _swap["dir"])


def _note(st):
    with open(_log, "a") as fh:
        fh.write(f"{st.st_ino}\n")


def _chown(path, uid, gid, *, dir_fd=None, follow_symlinks=True):
    _before(path)
    _note(_os.stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks))
    return _real_chown(path, uid, gid, dir_fd=dir_fd, follow_symlinks=follow_symlinks)


def _lchown(path, uid, gid):
    _before(path)
    _note(_os.lstat(path))
    return _real_lchown(path, uid, gid)


_os.chown, _os.lchown = _chown, _lchown
'''


def _restore_program() -> str:
    text = (SCRIPTS / "restore.sh").read_text()

    def block(start: str) -> str:
        body = text[text.index(start) + len(start):]
        return body[:body.index("\nPY\n")]
    return block("FILTER_PY=$(cat <<'PY'\n") + "\n" + block("RESTORE_PY=\"$FILTER_PY\"$(cat <<'PY'\n")


def test_the_restores_ownership_walk_follows_no_link(tmp_path):
    """Run as root (in a user namespace of this test's own), the walk that
    gives the restored tree its owner reaches every name from its folder's
    handle: a folder replaced by a link while the walk runs never sends it
    to the files the link points at."""
    import io
    import shutil
    import tarfile
    if shutil.which("unshare") is None or subprocess.run(
            ["unshare", "-Ur", "true"], capture_output=True).returncode != 0:
        pytest.skip("no user namespace here: the rig proves this walk")
    root = tmp_path / "tree"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f").write_text("not the tree's")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in (("a/x", b"x"), ("b/f", b"f")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    log = tmp_path / "chown.log"
    env = {**os.environ, "CHOWN_LOG": str(log),
           "CHOWN_SWAP": json.dumps({"dir": str(root / "b"), "to": str(outside)})}
    r = subprocess.run(["unshare", "-Ur", sys.executable, "-I", "-c", _CHOWN_SPY + _restore_program(),
                        str(root), "20261114"], input=buf.getvalue(), capture_output=True, env=env,
                       timeout=60)
    assert r.returncode == 0, r.stderr.decode()
    reached = {int(line) for line in log.read_text().split()}
    assert reached and os.stat(outside / "f").st_ino not in reached
