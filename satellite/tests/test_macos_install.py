"""The macOS half of ``install.sh``: the login item's PATH, the platform's
Python minor through Homebrew, and the load into the desktop session.

The shell functions run in bash on any host with their inputs set by hand;
the rest is read from the script's text."""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALL = Path(__file__).resolve().parents[1] / "install.sh"
TEXT = INSTALL.read_text(encoding="utf-8")


def _function(name: str) -> str:
    m = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", TEXT, re.S | re.M)
    assert m, name
    return m.group(0)


def _bash(script: str, **env: str) -> str:
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("no bash")
    r = subprocess.run([bash, "-c", "set -euo pipefail\n" + script], env={"PATH": "/usr/bin:/bin", **env},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.mark.parametrize("brew, want", [
    ("/opt/homebrew", "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/Users/p/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
    # Intel: Homebrew's bin is /usr/local/bin, listed once.
    ("/usr/local", "/usr/local/bin:/usr/local/sbin:/Users/p/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
    ("", "/usr/local/bin:/Users/p/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"),
])
def test_the_login_item_path_puts_homebrew_first(brew, want):
    out = _bash(f'BREW_PREFIX="{brew}"\n' + _function("macos_daemon_path") + "macos_daemon_path\n", HOME="/Users/p")
    assert out == want


@pytest.mark.parametrize("brew", ["/opt/homebrew", "/usr/local"])
def test_the_daemon_puts_the_login_items_tool_folders_first_too(brew):
    """A Mac paired before the login item carried its PATH gets the same
    folders from the daemon at start, on Apple silicon and on Intel."""
    from satellite.host import cli_versions
    out = _bash(f'BREW_PREFIX="{brew}"\n' + _function("macos_daemon_path") + "macos_daemon_path\n", HOME="/Users/p")
    tools = [d for d in out.split(":") if d not in ("/usr/bin", "/bin", "/usr/sbin", "/sbin")]
    daemon = [d.replace("~", "/Users/p", 1) for d in cli_versions._DARWIN_TOOL_DIRS]
    assert set(tools) <= set(daemon), set(tools) - set(daemon)


def test_the_plist_carries_that_path_and_starts_in_the_desktop_session():
    agent = TEXT.index("<key>Label</key>")
    plist = TEXT[TEXT.rindex('<?xml version="1.0"', 0, agent):TEXT.index("</plist>", agent)]
    assert "<key>EnvironmentVariables</key>" in plist
    assert "<key>PATH</key><string>$(macos_daemon_path)</string>" in plist
    assert 'launchctl bootstrap "gui/$UID_NUM" "$PLIST_FILE"' in TEXT
    # `launchctl load` from an SSH pairing lands the job outside the desktop
    # session: only the no-desktop fallback uses it.
    assert "launchctl load \"$PLIST_FILE\"" in TEXT.split('launchctl print "gui/$UID_NUM"')[-1]


def test_a_mac_prefers_the_platforms_minor_through_homebrews_opt_path():
    assert "$BREW_PREFIX/opt/python@$OTODOCK_PYTHON_MINOR/bin/python$OTODOCK_PYTHON_MINOR" in TEXT
    step2 = TEXT[TEXT.index("# --- Step 2: Python check ---"):TEXT.index("# --- Step 3")]
    assert step2.index("opt/python@") < step2.index("for cmd in python3 python; do")


def test_a_mac_without_the_platforms_python_is_warned_not_silently_moved_on():
    step2 = TEXT[TEXT.index("# --- Step 2: Python check ---"):TEXT.index("# --- Step 3")]
    mac = step2[:step2.index("for cmd in python3 python; do")]
    assert "warn \"Python $OTODOCK_PYTHON_MINOR not found. Install it with 'brew install python@$OTODOCK_PYTHON_MINOR'" in mac


def test_a_login_item_that_does_not_start_says_what_launchctl_said():
    load = TEXT[TEXT.index("started=false"):TEXT.index("in your desktop session\"")]
    assert 'said="$(launchctl bootstrap "gui/$UID_NUM" "$PLIST_FILE" 2>&1)"' in load
    assert 'err "The satellite\'s login item did not start. launchctl said: $said"' in load


def test_a_mac_stops_the_old_satellite_before_its_files_change():
    stop = TEXT.index("macos_stop_job\n", TEXT.index("# --- Step 4"))
    assert stop < TEXT.index('echo "$SATELLITE_TARBALL_B64" | base64 -d')
    body = _function("macos_stop_job")
    assert 'bootout "gui/$UID_NUM/$LAUNCHD_LABEL"' in body and 'bootout "user/$UID_NUM/$LAUNCHD_LABEL"' in body


def test_the_login_item_runs_the_app_when_it_was_built():
    step7 = TEXT[TEXT.index('elif [ "$OS_TYPE" = "macos" ]; then\n    mkdir -p "$HOME/Library/LaunchAgents"'):]
    assert '"$SATELLITE_DIR/venv/bin/python" "$SATELLITE_DIR/satellite/host/macos_app.py" "$OTO_DIR"' in step7
    assert 'PROGRAM_ARGS="        <string>$APP_EXE</string>"' in step7
    assert "<key>AssociatedBundleIdentifiers</key><string>$LAUNCHD_LABEL</string>" in step7


# ── uninstall.sh: the otodock command's link and the Mac's login item ─────

UNINSTALL = INSTALL.parent / "uninstall.sh"


def test_the_uninstall_removes_only_its_own_otodock_link(tmp_path):
    text = UNINSTALL.read_text(encoding="utf-8")
    fn = re.search(r"^remove_cli_link\(\) \{\n.*?^\}\n", text, re.S | re.M).group(0)
    home, oto = tmp_path / "home", tmp_path / "home" / ".oto-dock"
    bindir = home / ".local" / "bin"
    bindir.mkdir(parents=True)
    script = f'OTO_DIR="{oto}"\nok() {{ echo "[OK] $*"; }}\n' + fn + "remove_cli_link\n"
    (bindir / "otodock").symlink_to(oto / "satellite" / "bin" / "otodock")  # dangling, as after the wipe
    _bash(script, HOME=str(home))
    assert not (bindir / "otodock").is_symlink()
    (bindir / "otodock").symlink_to(tmp_path / "elsewhere" / "otodock")
    _bash(script, HOME=str(home))
    assert (bindir / "otodock").is_symlink()


def test_the_uninstall_stops_the_mac_login_item_in_both_domains_and_clears_its_grants():
    text = UNINSTALL.read_text(encoding="utf-8")
    darwin = text[text.index('elif [ "$OS" = "Darwin" ]; then\n    LABEL='):]
    assert 'launchctl bootout "gui/$(id -u)/$LABEL"' in darwin
    assert 'launchctl bootout "user/$(id -u)/$LABEL"' in darwin
    assert 'tccutil reset Accessibility "$LABEL"' in darwin and 'tccutil reset ScreenCapture "$LABEL"' in darwin


def test_the_uninstall_names_pnpm_once_per_system():
    """A Mac's pnpm is Homebrew's, a Linux host's npm's: the cleanup hint
    names it in one remove command on each."""
    text = UNINSTALL.read_text(encoding="utf-8")
    hint = text[text.index("For a full cleanup"):text.index("To re-pair this machine")]
    mac, linux = hint.split("\nelse\n")
    assert "brew uninstall" in mac and "pnpm" in mac.split("npm uninstall -g")[0]
    assert "npm uninstall -g" in mac and "pnpm" not in mac.split("npm uninstall -g")[1]
    assert "pnpm" not in linux.split("npm uninstall -g")[0] and "pnpm" in linux.split("npm uninstall -g")[1]


def test_the_uninstall_reads_its_config_with_posix_classes_only(tmp_path):
    """macOS sed reads ``\\s`` as a literal s (a manual uninstall there sent a
    mangled URL and never told the platform): the three config reads use
    ``[[:space:]]`` and parse a value with blanks around it whole."""
    text = UNINSTALL.read_text(encoding="utf-8")
    reads = re.search(r"^    MACHINE_ID=\$\(grep[^\n]*\n    MACHINE_SECRET=[^\n]*\n    PLATFORM_WSS=[^\n]*\n",
                      text, re.M).group(0)
    assert "\\s" not in reads
    conf = tmp_path / "satellite.conf"
    conf.write_text("[satellite]\nmachine_id =  abc-123 \nmachine_secret= secretends\n"
                    "platform_url = wss://example.invalid/v1/satellite\n", encoding="utf-8")
    out = _bash(f'CONF="{conf}"\n' + reads + 'echo "$MACHINE_ID|$MACHINE_SECRET|$PLATFORM_WSS"\n')
    assert out == "abc-123|secretends|wss://example.invalid/v1/satellite"
