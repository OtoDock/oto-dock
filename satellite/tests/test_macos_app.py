"""OtoDock Satellite.app (``host/macos_app.py``): the launcher, compiled and
run on this host in a fake install, and the build that keeps a signed bundle
(and so the Mac's grants) when nothing it is made from changed."""
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from satellite.host import macos_app

BIN = Path(__file__).resolve().parents[1] / "bin"


# ── the launcher ──────────────────────────────────────────────────────────


@pytest.fixture
def launcher(tmp_path):
    """The launcher compiled into ``<root>/OtoDock Satellite.app/Contents/
    MacOS/OtoDock Satellite`` beside a fake venv python."""
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler")
    src = tmp_path / "launcher.c"
    src.write_text(macos_app.LAUNCHER_C)
    exe = macos_app.app_dir(tmp_path / "root") / "Contents" / "MacOS" / macos_app.APP_NAME
    exe.parent.mkdir(parents=True)
    r = subprocess.run([cc, "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(exe), str(src)],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    py = tmp_path / "root" / "satellite" / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    return exe, py, tmp_path / "ran.log"


def _job_env(log: Path) -> dict:
    return {"PATH": "/usr/bin:/bin", "XPC_SERVICE_NAME": macos_app.LABEL, "RAN_LOG": str(log)}


def test_the_launcher_runs_the_venvs_python_and_returns_its_status(launcher):
    exe, py, log = launcher
    py.write_text('#!/bin/sh\necho "$0 $*" > "$RAN_LOG"\nexit 3\n')
    py.chmod(0o755)
    r = subprocess.run([str(exe)], env=_job_env(log), capture_output=True, text=True, timeout=30)
    assert r.returncode == 3, r.stderr
    # The venv's python is named as it is, never resolved out of the venv.
    assert log.read_text().strip() == f"{py} -m satellite"


def test_the_launcher_hands_a_stop_to_the_satellite(launcher):
    exe, py, log = launcher
    py.write_text('#!/bin/sh\ntrap \'echo stopped > "$RAN_LOG"; exit 7\' TERM\necho up > "$RAN_LOG"\n'
                  'while :; do sleep 0.05; done\n')
    py.chmod(0o755)
    proc = subprocess.Popen([str(exe)], env=_job_env(log))
    for _ in range(100):
        if log.exists() and log.read_text().strip() == "up":
            break
        subprocess.run(["sleep", "0.05"], check=False)
    proc.terminate()
    assert proc.wait(timeout=10) == 7
    assert log.read_text().strip() == "stopped"


def test_the_launcher_opened_by_hand_starts_no_second_satellite(launcher):
    exe, py, log = launcher
    py.write_text('#!/bin/sh\necho ran > "$RAN_LOG"\n')
    py.chmod(0o755)
    env = _job_env(log)
    env.pop("XPC_SERVICE_NAME")
    r = subprocess.run([str(exe)], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and not log.exists()


def test_the_apps_icon_is_the_256_px_frame_of_the_windows_icon():
    png = macos_app.icon_png((BIN / "otodock.ico").read_bytes())
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert struct.unpack(">II", png[16:24]) == (256, 256)


# ── the build: kept while its inputs hold, the grants cleared on a rebuild ─


class _Mac:
    """``cc``, ``sips``, ``codesign``, ``tccutil`` and ``lsregister`` as
    recorded calls; the compiler writes the executable it is asked for."""

    def __init__(self, monkeypatch, *, cc_ok=True, no_clt=False):
        self.calls: list[list[str]] = []
        self.cc_ok, self.no_clt = cc_ok, no_clt
        monkeypatch.setattr(macos_app.shutil, "which", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(macos_app.subprocess, "run", self.run)

    def run(self, cmd, **kw):
        self.calls.append(list(cmd))
        ok = (self.cc_ok or cmd[0] != "cc") and not (self.no_clt and cmd[0] == "xcode-select")
        if cmd[0] == "cc" and ok:
            out = Path(cmd[cmd.index("-o") + 1])
            out.write_text("binary")
            out.chmod(0o755)
        return subprocess.CompletedProcess(cmd, 0 if ok else 1)

    def ran(self, tool):
        return [c for c in self.calls if c[0] == tool or c[0].endswith("/" + tool)]


@pytest.fixture
def install(tmp_path):
    oto = tmp_path / ".oto-dock"
    (oto / "satellite" / "bin").mkdir(parents=True)
    shutil.copy(BIN / "otodock.ico", oto / "satellite" / "bin" / "otodock.ico")
    return oto


def test_a_build_signs_the_bundle_and_a_second_run_keeps_it(monkeypatch, install):
    mac = _Mac(monkeypatch)
    said: list[str] = []
    assert macos_app.build(install, install / "satellite", said.append)
    app = macos_app.app_dir(install)
    assert (app / "Contents" / "Info.plist").read_text() == macos_app.INFO_PLIST
    # Signed where it was built, then moved in whole.
    [sign] = mac.ran("codesign")
    assert sign[:6] == ["codesign", "--force", "--sign", "-", "--identifier", macos_app.LABEL]
    assert sign[6].endswith("/OtoDock Satellite.app") and (app / "Contents" / "MacOS" / macos_app.APP_NAME).is_file()
    assert not mac.ran("tccutil")  # a first build has no grants to clear
    assert said[-1] == "[OK] OtoDock Satellite.app built"

    before = len(mac.calls)
    assert macos_app.build(install, install / "satellite", said.append)
    assert len(mac.calls) == before  # nothing compiled, signed or reset
    assert said[-1] == "[OK] OtoDock Satellite.app is current (its macOS permissions stay)"


def test_a_changed_input_rebuilds_and_clears_the_old_grants(monkeypatch, install):
    mac = _Mac(monkeypatch)
    assert macos_app.build(install, install / "satellite", lambda s: None)
    (install / macos_app.STAMP_FILE).write_text("an older launcher\n")
    assert macos_app.build(install, install / "satellite", lambda s: None)
    assert mac.ran("tccutil") == [["tccutil", "reset", "Accessibility", macos_app.LABEL],
                                  ["tccutil", "reset", "ScreenCapture", macos_app.LABEL]]


def test_a_mac_that_cannot_build_runs_without_the_app(monkeypatch, install):
    _Mac(monkeypatch, cc_ok=False)
    said: list[str] = []
    assert macos_app.build(install, install / "satellite", said.append) is False
    assert not macos_app.app_dir(install).exists()
    assert not (install / macos_app.STAMP_FILE).exists()
    monkeypatch.setattr(macos_app.shutil, "which", lambda name: None)
    assert macos_app.build(install, install / "satellite", said.append) is False
    assert said[-1].startswith("[WARN] No Command Line Tools or codesign on this Mac.")


def test_a_mac_without_the_command_line_tools_never_runs_the_compiler(monkeypatch, install):
    # /usr/bin/cc is a stub there that opens Apple's install dialog.
    mac = _Mac(monkeypatch, no_clt=True)
    assert macos_app.build(install, install / "satellite", lambda s: None) is False
    assert not mac.ran("cc") and mac.ran("xcode-select") == [["xcode-select", "-p"]]


# The launcher and the Info.plist, frozen: their hash decides a rebuild, and a
# rebuild makes every Mac ask for its two permissions again.
FROZEN_STAMP = "b87877d787293ed87c61fac298cfba5fdb43fcff16d3d5f84703764d39288333"


def test_the_apps_inputs_are_frozen():
    assert macos_app.stamp() == FROZEN_STAMP, (
        "OtoDock Satellite.app's launcher or Info.plist changed. Every Mac then asks for its "
        "permissions again at its next pairing: write an upgrade note in CHANGELOG.md, then "
        "update FROZEN_STAMP.")


def test_a_new_icon_alone_rebuilds_nothing(monkeypatch, install):
    mac = _Mac(monkeypatch)
    assert macos_app.build(install, install / "satellite", lambda s: None)
    (install / "satellite" / "bin" / "otodock.ico").write_bytes(b"a redrawn mark")
    before = len(mac.calls)
    assert macos_app.build(install, install / "satellite", lambda s: None)
    assert len(mac.calls) == before


def test_the_info_plist_carries_its_version_and_the_privacy_sentences():
    import plistlib
    info = plistlib.loads(macos_app.INFO_PLIST.encode())
    assert info["CFBundleIdentifier"] == "com.otodock.satellite" and info["CFBundleShortVersionString"]
    for key in ("NSAppleEventsUsageDescription", "NSDesktopFolderUsageDescription",
                "NSDocumentsFolderUsageDescription", "NSDownloadsFolderUsageDescription"):
        assert info[key].endswith(".") and ";" not in info[key], key


def test_the_icon_is_found_where_the_tarball_and_a_repo_copy_put_it(tmp_path):
    sat = tmp_path / "satellite"
    assert macos_app.icon_source(sat) is None
    (sat / "satellite" / "bin").mkdir(parents=True)
    (sat / "satellite" / "bin" / "otodock.ico").write_bytes(b"x")
    assert macos_app.icon_source(sat) == sat / "satellite" / "bin" / "otodock.ico"
    (sat / "bin").mkdir()
    (sat / "bin" / "otodock.ico").write_bytes(b"y")
    assert macos_app.icon_source(sat) == sat / "bin" / "otodock.ico"
