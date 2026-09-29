"""The host OS table — ``core/host_os.py`` — its twin in ``satellite/config.py``
(the boot-guard leaf that cannot import a vendored copy; the gate's twin rule
pins the three functions, this file pins every value) and the dashboard's
``lib/hostOs/os.ts``. Core-seams phase 10."""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import sys

import pytest

from core import host_os
from tests._paths import PROXY_DIR, REPO_ROOT

_MIRROR = REPO_ROOT / "dashboard" / "src" / "lib" / "hostOs" / "os.ts"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from satellite import config as sat_config  # noqa: E402


# ---------------------------------------------------------------------------
# the rows
# ---------------------------------------------------------------------------

def test_the_wire_words_are_frozen():
    assert host_os.FAMILIES == ("linux", "darwin", "windows")
    assert {r.name for w, r in host_os.ROWS.items() if w} == set(host_os.FAMILIES)
    assert host_os.OTHER.name == ""


def test_the_rows_spell_what_the_sites_spelled():
    linux, darwin, windows = (host_os.ROWS[w] for w in host_os.FAMILIES)
    assert (linux.dirname, darwin.dirname, windows.dirname) == (".oto-dock", ".oto-dock", "OtoDock")
    assert (linux.exe_suffix, windows.exe_suffix) == ("", ".exe")
    assert (linux.venv_bin, windows.venv_bin) == ("bin", "Scripts")
    assert (linux.python_exe, windows.python_exe) == ("python3", "python")
    assert (linux.base_python, windows.base_python) == (("bin", "python3"), ("python.exe",))
    assert (linux.posix, darwin.posix, windows.posix) == (True, True, False)
    assert (linux.peercred, darwin.peercred, windows.peercred) == (True, False, False)
    assert (linux.case_insensitive, darwin.case_insensitive, windows.case_insensitive) == (False, True, True)
    assert (linux.conpty, windows.conpty) == (False, True)
    assert (linux.locks_running_files, windows.locks_running_files) == (False, True)
    assert (linux.service_manager, darwin.service_manager, windows.service_manager) == (
        host_os.SERVICE_SYSTEMD, host_os.SERVICE_LAUNCHD, host_os.SERVICE_SCHTASKS)
    assert (linux.powershell, windows.powershell) == (False, True)
    assert (linux.script_suffix, windows.script_suffix) == (".sh", ".ps1")
    assert (linux.tray, windows.tray) == (False, True)
    assert (linux.display_server, darwin.display_server, windows.display_server) == (
        "", host_os.DISPLAY_QUARTZ, host_os.DISPLAY_WINDOWS)
    assert (linux.xdg_user_dirs, darwin.xdg_user_dirs, windows.xdg_user_dirs) == (True, False, False)
    assert (linux.videos_folder, darwin.videos_folder, windows.videos_folder) == ("Videos", "Movies", "Videos")
    other = host_os.OTHER
    assert other.posix and not other.peercred and other.service_manager == "" \
        and other.display_server == host_os.DISPLAY_NONE and not other.xdg_user_dirs \
        and other.videos_folder == "Videos" and other.dirname == ".oto-dock"


def test_the_display_words_are_frozen():
    assert host_os.DISPLAY_SERVERS == ("x11", "wayland", "quartz", "windows", "none")


# ---------------------------------------------------------------------------
# the questions
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("plat, fam", [
    ("win32", "windows"), ("darwin", "darwin"), ("linux", "linux"), ("linux2", "linux"),
    ("cygwin", ""), ("freebsd13", ""), ("", ""),
])
def test_family_of(plat, fam):
    assert host_os.family_of(plat) == fam


def test_host_os_answers_this_interpreter(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert host_os.host_os() is host_os.ROWS["windows"]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert host_os.host_os() is host_os.ROWS["darwin"]
    monkeypatch.setattr(sys, "platform", "linux")
    assert host_os.host_os() is host_os.ROWS["linux"]


def test_host_os_outside_the_families_keeps_the_wire_word(monkeypatch):
    monkeypatch.setattr(sys, "platform", "freebsd13")
    import platform as _platform
    monkeypatch.setattr(_platform, "system", lambda: "FreeBSD")
    row = host_os.host_os()
    assert row.name == "freebsd"
    assert dataclasses.replace(row, name="") == host_os.OTHER
    assert row is not host_os.ROWS["linux"]


@pytest.mark.parametrize("word, expect", [
    ("linux", "linux"), (" Linux ", "linux"), ("DARWIN", "darwin"), ("windows", "windows"),
    ("", None), (None, None), ("plan9", None), ("macos", None), ("win32", None),
])
def test_of_folds_and_refuses(word, expect):
    row = host_os.of(word)
    assert (row.name if row else None) == expect


@pytest.mark.parametrize("word, fam", [
    ("linux", "linux"), ("unix", "linux"), ("macos", "darwin"),
    ("windows", "windows"), ("win", "windows"), ("win32", "windows"),
    ("bsd", ""), ("", ""), ("darwin", ""),
])
def test_bootstrap_family(word, fam):
    assert host_os.bootstrap_family(word) == fam


def test_bootstrap_flavours_are_the_modal_s_keys():
    assert host_os.BOOTSTRAP_FLAVORS == ("linux", "macos", "windows")


# ---------------------------------------------------------------------------
# translate_venv — the rewrite's two moved tests and the identity
# ---------------------------------------------------------------------------

WIN = host_os.ROWS["windows"]


@pytest.mark.parametrize("row", [host_os.ROWS["linux"], host_os.ROWS["darwin"], host_os.OTHER])
def test_translate_venv_is_the_identity_for_a_posix_row(row):
    for s in ("~/.oto-dock/mcps/core/x/venv/bin/python3", "/home/foo/venv/bin/workspace-mcp", "bare"):
        assert host_os.translate_venv(s, row) == s


def test_translate_venv_windows():
    assert host_os.translate_venv("~/OtoDock/mcps/core/x/venv/bin/python3", WIN) == \
        "~/OtoDock/mcps/core/x/venv/Scripts/python.exe"
    assert host_os.translate_venv("~/OtoDock/mcps/c/x/venv/bin/workspace-mcp", WIN) == \
        "~/OtoDock/mcps/c/x/venv/Scripts/workspace-mcp.exe"


def test_translate_venv_idempotent_on_scripts_layout():
    s = "C:/foo/venv/Scripts/python.exe"
    assert host_os.translate_venv(s, WIN) == s


def test_translate_venv_does_not_double_suffix():
    s = "/home/foo/venv/bin/workspace-mcp.exe"
    assert host_os.translate_venv(s, WIN) == "/home/foo/venv/Scripts/workspace-mcp.exe"


# ---------------------------------------------------------------------------
# the twin in satellite/config.py
# ---------------------------------------------------------------------------

def test_the_satellite_twin_carries_the_same_table():
    assert [f.name for f in dataclasses.fields(sat_config.HostOS)] == \
        [f.name for f in dataclasses.fields(host_os.HostOS)]
    assert set(sat_config.ROWS) == set(host_os.ROWS)
    for word, row in host_os.ROWS.items():
        assert dataclasses.asdict(sat_config.ROWS[word]) == dataclasses.asdict(row), word
    assert sat_config.FAMILIES == host_os.FAMILIES
    assert sat_config.DISPLAY_SERVERS == host_os.DISPLAY_SERVERS
    assert (sat_config.SERVICE_SYSTEMD, sat_config.SERVICE_LAUNCHD, sat_config.SERVICE_SCHTASKS) == (
        host_os.SERVICE_SYSTEMD, host_os.SERVICE_LAUNCHD, host_os.SERVICE_SCHTASKS)
    for plat in ("win32", "darwin", "linux", "linux2", "cygwin", ""):
        assert sat_config.family_of(plat) == host_os.family_of(plat)
    for word in ("linux", " Windows ", "", "plan9"):
        a, b = sat_config.of(word), host_os.of(word)
        assert (a is None) == (b is None) and (a is None or a.name == b.name)


def test_the_satellite_binds_this_host_once():
    assert sat_config.HOST.name == host_os.host_os().name
    assert sat_config.OTODOCK_DIRNAME == sat_config.HOST.dirname
    assert sat_config.EXE_SUFFIX == sat_config.HOST.exe_suffix


def test_the_satellite_config_stays_a_leaf():
    script = ("import sys, json\nimport satellite.config\n"
              "print(json.dumps(sorted(m for m in sys.modules if m.startswith('satellite.'))))\n")
    out = subprocess.run([sys.executable, "-c", script], cwd=str(REPO_ROOT),
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == ["satellite.config", "satellite.engines"]


def test_the_proxy_leaf_imports_nothing_of_the_tree():
    script = ("import sys, json\nimport core.host_os\n"
              "print(json.dumps(sorted(m for m in sys.modules if m.startswith("
              "('core.', 'services.', 'storage.', 'auth', 'config', 'ws.', 'api.')))))\n")
    out = subprocess.run([sys.executable, "-c", script], cwd=str(PROXY_DIR),
                         capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == ["core.host_os"]


# ---------------------------------------------------------------------------
# the dashboard mirror
# ---------------------------------------------------------------------------

def _ts_const_strings(text: str, name: str) -> list[str]:
    m = re.search(rf"export const {name}\b[^=]*=\s*(.+?)(?:\n\n|\nexport|\Z)", text, re.S)
    assert m, name
    return re.findall(r"'([^']*)'", m.group(1))


def test_the_dashboard_mirror_is_in_lock_step():
    text = _MIRROR.read_text(encoding="utf-8")
    assert _ts_const_strings(text, "SATELLITE_OS") == list(host_os.FAMILIES)
    assert _ts_const_strings(text, "BOOTSTRAP_OS") == list(host_os.BOOTSTRAP_FLAVORS)
    assert _ts_const_strings(text, "DISPLAY_SERVER") == list(host_os.DISPLAY_SERVERS)
