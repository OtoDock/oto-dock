"""Tests for the satellite capabilities probe extensions.

Covers ``_detect_os_user_and_home`` + ``_detect_user_dirs`` from
``satellite/session_manager.py``. Per-OS branches: Linux (with and
without an XDG user-dirs file), macOS, Windows (mocked via
``platform.system``).
"""

import sys


# Add repo root to path so we can import satellite/* directly.
from tests._paths import REPO_ROOT as _REPO_ROOT
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from satellite.sessions.session_manager import (  # noqa: E402
    _detect_os_user_and_home,
    _detect_user_dirs,
)


class TestDetectOsUserAndHome:
    def test_returns_strings(self):
        os_user, home_dir = _detect_os_user_and_home()
        assert isinstance(os_user, str)
        assert isinstance(home_dir, str)
        assert home_dir  # always non-empty on real systems

    def test_home_is_absolute(self):
        _, home_dir = _detect_os_user_and_home()
        # Forward-slash form on every OS — verify by checking it doesn't
        # contain backslashes (we explicitly normalize for Windows).
        assert "\\" not in home_dir


class TestDetectUserDirsLinux:
    def test_xdg_file_parsed(self, tmp_path, monkeypatch):
        # Build a synthetic home with a custom XDG user-dirs file.
        home = tmp_path / "myhome"
        (home / ".config").mkdir(parents=True)
        xdg = home / ".config" / "user-dirs.dirs"
        xdg.write_text(
            '# This file is written by xdg-user-dirs-update\n'
            'XDG_DESKTOP_DIR="$HOME/CustomDesktop"\n'
            'XDG_DOWNLOAD_DIR="$HOME/CustomDownloads"\n'
            'XDG_DOCUMENTS_DIR="$HOME/Papers"\n'
            'XDG_PICTURES_DIR="$HOME/Pictures"\n'
            'XDG_MUSIC_DIR="$HOME/Tunes"\n'
            'XDG_VIDEOS_DIR="$HOME/Vids"\n'
        )
        monkeypatch.setattr("platform.system", lambda: "Linux")
        dirs = _detect_user_dirs(str(home))
        assert dirs["desktop"]   == f"{home}/CustomDesktop"
        assert dirs["downloads"] == f"{home}/CustomDownloads"
        assert dirs["documents"] == f"{home}/Papers"
        assert dirs["pictures"]  == f"{home}/Pictures"
        assert dirs["music"]     == f"{home}/Tunes"
        assert dirs["videos"]    == f"{home}/Vids"

    def test_missing_xdg_uses_defaults(self, tmp_path, monkeypatch):
        home = tmp_path / "newhome"
        home.mkdir()
        monkeypatch.setattr("platform.system", lambda: "Linux")
        dirs = _detect_user_dirs(str(home))
        # All six keys present with conventional layout.
        assert dirs["desktop"]   == str(home / "Desktop")
        assert dirs["downloads"] == str(home / "Downloads")
        assert dirs["documents"] == str(home / "Documents")
        assert dirs["pictures"]  == str(home / "Pictures")
        assert dirs["music"]     == str(home / "Music")
        assert dirs["videos"]    == str(home / "Videos")

    def test_xdg_with_partial_keys(self, tmp_path, monkeypatch):
        # XDG file declares only Desktop + Downloads; the rest fall back.
        home = tmp_path / "partial"
        (home / ".config").mkdir(parents=True)
        (home / ".config" / "user-dirs.dirs").write_text(
            'XDG_DESKTOP_DIR="$HOME/CustomDesktop"\n'
            'XDG_DOWNLOAD_DIR="$HOME/CustomDownloads"\n'
        )
        monkeypatch.setattr("platform.system", lambda: "Linux")
        dirs = _detect_user_dirs(str(home))
        assert dirs["desktop"]   == f"{home}/CustomDesktop"
        assert dirs["downloads"] == f"{home}/CustomDownloads"
        assert dirs["documents"] == str(home / "Documents")
        assert dirs["pictures"]  == str(home / "Pictures")

    def test_xdg_skips_unknown_keys(self, tmp_path, monkeypatch):
        # Unknown XDG keys (PUBLICSHARE, TEMPLATES) are ignored.
        home = tmp_path / "unknown"
        (home / ".config").mkdir(parents=True)
        (home / ".config" / "user-dirs.dirs").write_text(
            'XDG_TEMPLATES_DIR="$HOME/Templates"\n'
            'XDG_PUBLICSHARE_DIR="$HOME/Public"\n'
            'XDG_DESKTOP_DIR="$HOME/Desktop"\n'
        )
        monkeypatch.setattr("platform.system", lambda: "Linux")
        dirs = _detect_user_dirs(str(home))
        assert "templates" not in dirs
        assert "publicshare" not in dirs
        assert "public" not in dirs
        assert dirs["desktop"] == f"{home}/Desktop"


class TestDetectUserDirsMacOS:
    def test_fixed_layout(self, tmp_path, monkeypatch):
        home = "/Users/dave"
        monkeypatch.setattr("platform.system", lambda: "Darwin")
        dirs = _detect_user_dirs(home)
        assert dirs["desktop"]   == "/Users/dave/Desktop"
        assert dirs["downloads"] == "/Users/dave/Downloads"
        assert dirs["documents"] == "/Users/dave/Documents"
        assert dirs["pictures"]  == "/Users/dave/Pictures"
        assert dirs["music"]     == "/Users/dave/Music"
        # macOS-specific: videos folder is named Movies.
        assert dirs["videos"]    == "/Users/dave/Movies"


class TestDetectUserDirsWindows:
    def test_fixed_layout_forward_slash(self, monkeypatch):
        home = "c:/Users/dave"
        monkeypatch.setattr("platform.system", lambda: "Windows")
        dirs = _detect_user_dirs(home)
        assert dirs["desktop"]   == "c:/Users/dave/Desktop"
        assert dirs["downloads"] == "c:/Users/dave/Downloads"
        assert dirs["documents"] == "c:/Users/dave/Documents"
        assert dirs["pictures"]  == "c:/Users/dave/Pictures"
        assert dirs["music"]     == "c:/Users/dave/Music"
        assert dirs["videos"]    == "c:/Users/dave/Videos"

    def test_no_backslashes(self, monkeypatch):
        home = "c:/Users/dave"
        monkeypatch.setattr("platform.system", lambda: "Windows")
        dirs = _detect_user_dirs(home)
        for value in dirs.values():
            assert "\\" not in value
