"""OtoDock Satellite.app, the satellite's login item on macOS.

macOS names the process that asks for a permission. The LaunchAgent runs a
small launcher inside an app bundle that starts the satellite's Python as its
child and stays its parent, so every permission the satellite, its engines and
its tools ask for lists as "OtoDock Satellite" (an exec would hand them to
Python's own app). ``install.sh`` builds it on the Mac with the Command Line
Tools: ``venv/bin/python satellite/host/macos_app.py <install folder>``.

An ad hoc grant is tied to the bundle's signature, which covers its Info.plist
and every resource. The bundle is rebuilt only when the launcher or the
Info.plist changes (a stamp outside it), so a re-pair keeps the grants, and a
rebuild clears the old grants rather than leave a switch on in System Settings
that no longer applies. THE TWO ARE FROZEN: a change to either makes every Mac
ask for its permissions again at its next pairing, so it needs an upgrade note
(a test pins the stamp). The icon is not an input: a new brand mark reaches a
bundle only when it is rebuilt for another reason. Stdlib only: it runs as a
script, before the satellite starts.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import struct
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

LABEL = "com.otodock.satellite"
APP_NAME = "OtoDock Satellite"
STAMP_FILE = ".satellite-app.sha256"
LSREGISTER = ("/System/Library/Frameworks/CoreServices.framework/Frameworks/"
              "LaunchServices.framework/Support/lsregister")

LAUNCHER_C = r"""/* The login item of OtoDock Satellite on macOS: launchd starts this program,
   which starts the satellite's Python as its child and stays its parent, so
   macOS credits every permission asked below it to this app. */
#include <errno.h>
#include <limits.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif

#define LABEL "com.otodock.satellite"

extern char **environ;
static volatile pid_t child = -1;

static void forward(int sig) {
    if (child > 0) kill(child, sig);
}

static int own_path(char *out) {
    char raw[PATH_MAX];
#ifdef __APPLE__
    uint32_t size = sizeof raw;
    if (_NSGetExecutablePath(raw, &size) != 0) return -1;
#else
    ssize_t n = readlink("/proc/self/exe", raw, sizeof raw - 1);
    if (n < 0) return -1;
    raw[n] = '\0';
#endif
    return realpath(raw, out) ? 0 : -1;
}

int main(void) {
    const char *job = getenv("XPC_SERVICE_NAME");
    int login_item = job != NULL && strcmp(job, LABEL) == 0;
#ifdef __APPLE__
    /* Every process below the satellite inherits the label: only the one
       launchd started (its parent is pid 1) is the login item. */
    login_item = login_item && getppid() == 1;
#endif
    if (!login_item) {
        /* Opened from Finder, `open` or macOS's Quit & Reopen: start the
           login item when it is not running, never a second satellite. */
        char target[64];
        pid_t pid;
        snprintf(target, sizeof target, "gui/%d/%s", (int)getuid(), LABEL);
        char *kick[] = {"/bin/launchctl", "kickstart", target, NULL};
        if (posix_spawn(&pid, kick[0], NULL, NULL, kick, environ) == 0) waitpid(pid, NULL, 0);
        return 0;
    }

    /* <install>/OtoDock Satellite.app/Contents/MacOS/<this>: four levels up.
       Only this path is resolved: the venv's python is a link that must stay
       one, or Python starts outside its venv. */
    char root[PATH_MAX], python[PATH_MAX];
    if (own_path(root) != 0) {
        perror("OtoDock Satellite: own path");
        return 1;
    }
    for (int i = 0; i < 4; i++) {
        char *slash = strrchr(root, '/');
        if (slash == NULL || slash == root) {
            fprintf(stderr, "OtoDock Satellite: not inside its app bundle\n");
            return 1;
        }
        *slash = '\0';
    }
    int len = snprintf(python, sizeof python, "%s/satellite/venv/bin/python", root);
    if (len < 0 || (size_t)len >= sizeof python) {
        fprintf(stderr, "OtoDock Satellite: the install path is too long\n");
        return 1;
    }
    char *args[] = {python, "-m", "satellite", NULL};

    /* SIGTERM and SIGINT (the two the satellite handles) go to the child.
       Blocked until the child exists, so none is lost in between. */
    sigset_t forwarded, none, all;
    sigemptyset(&forwarded);
    sigaddset(&forwarded, SIGTERM);
    sigaddset(&forwarded, SIGINT);
    sigemptyset(&none);
    sigfillset(&all);
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = forward;
    sigemptyset(&sa.sa_mask);
    sigprocmask(SIG_BLOCK, &forwarded, NULL);
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    posix_spawnattr_t attr;
    posix_spawnattr_init(&attr);
    posix_spawnattr_setsigmask(&attr, &none);
    posix_spawnattr_setsigdefault(&attr, &all);
    posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF);
    pid_t pid;
    int rc = posix_spawn(&pid, python, NULL, &attr, args, environ);
    posix_spawnattr_destroy(&attr);
    if (rc != 0) {
        fprintf(stderr, "OtoDock Satellite: cannot start %s: %s\n", python, strerror(rc));
        return 1;
    }
    child = pid;
    sigprocmask(SIG_UNBLOCK, &forwarded, NULL);

    int status;
    while (waitpid(pid, &status, 0) < 0) {
        if (errno != EINTR) return 1;
    }
    if (WIFSIGNALED(status)) {
        signal(WTERMSIG(status), SIG_DFL);
        raise(WTERMSIG(status));
        return 128 + WTERMSIG(status);
    }
    return WIFEXITED(status) ? WEXITSTATUS(status) : 1;
}
"""

INFO_PLIST = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleIdentifier</key><string>{LABEL}</string>
    <key>CFBundleName</key><string>{APP_NAME}</string>
    <key>CFBundleDisplayName</key><string>{APP_NAME}</string>
    <key>CFBundleExecutable</key><string>{APP_NAME}</string>
    <key>CFBundleIconFile</key><string>AppIcon</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSUIElement</key><true/>
    <key>NSAppleEventsUsageDescription</key><string>Your agents can control other apps on this Mac when you ask them to.</string>
    <key>NSDesktopFolderUsageDescription</key><string>Your agents can work with the files on your desktop when you ask them to.</string>
    <key>NSDocumentsFolderUsageDescription</key><string>Your agents can work with the files in your Documents folder when you ask them to.</string>
    <key>NSDownloadsFolderUsageDescription</key><string>Your agents can work with the files in your Downloads folder when you ask them to.</string>
    <key>NSNetworkVolumesUsageDescription</key><string>Your agents can work with the files on network drives when you ask them to.</string>
    <key>NSRemovableVolumesUsageDescription</key><string>Your agents can work with the files on external drives when you ask them to.</string>
</dict>
</plist>
"""


def app_dir(oto_dir: Path) -> Path:
    return oto_dir / f"{APP_NAME}.app"


def icon_png(ico: bytes) -> bytes:
    """The largest frame of a Windows icon whose frames are PNGs (the
    satellite's own ``otodock.ico``)."""
    count = struct.unpack_from("<H", ico, 4)[0]
    frames = [struct.unpack_from("<BBBBHHII", ico, 6 + 16 * i) for i in range(count)]
    best = max(frames, key=lambda f: f[0] or 256)
    return ico[best[7]:best[7] + best[6]]


def icon_source(satellite_dir: Path) -> Path | None:
    """The tarball puts ``bin/`` beside the package, a repo copy inside it."""
    for path in (satellite_dir / "bin" / "otodock.ico", satellite_dir / "satellite" / "bin" / "otodock.ico"):
        if path.is_file():
            return path
    return None


def stamp() -> str:
    """What decides a rebuild: the launcher and the Info.plist (frozen, see
    the module's note), never the icon."""
    h = hashlib.sha256(LAUNCHER_C.encode())
    h.update(INFO_PLIST.encode())
    return h.hexdigest()


def _ran(cmd: list[str]) -> bool:
    return subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0


def build(oto_dir: Path, satellite_dir: Path, say: Callable[[str], None] = print) -> bool:
    """Build the app, or keep it when its stamp matches. ``False`` when this
    Mac cannot build it (no Command Line Tools or no codesign): the login item
    then runs the satellite's Python itself."""
    app = app_dir(oto_dir)
    stamp_file = oto_dir / STAMP_FILE
    icon = icon_source(satellite_dir)
    want = stamp()
    exe = app / "Contents" / "MacOS" / APP_NAME
    if os.access(exe, os.X_OK) and stamp_file.is_file() and stamp_file.read_text().strip() == want:
        say(f"[OK] {APP_NAME}.app is current (its macOS permissions stay)")
        return True
    # /usr/bin/cc exists on every Mac: without the Command Line Tools it is a
    # stub that opens Apple's install dialog on the person's screen.
    if shutil.which("codesign") is None or not _ran(["xcode-select", "-p"]):
        say("[WARN] No Command Line Tools or codesign on this Mac. The satellite runs without its app, "
            "and macOS lists its permissions under Python.")
        return False
    with tempfile.TemporaryDirectory() as scratch:
        tmp = Path(scratch)
        new = tmp / f"{APP_NAME}.app"
        (new / "Contents" / "MacOS").mkdir(parents=True)
        (new / "Contents" / "Resources").mkdir()
        (new / "Contents" / "Info.plist").write_text(INFO_PLIST)
        (tmp / "launcher.c").write_text(LAUNCHER_C)
        if not _ran(["cc", "-O2", "-Wall", "-o", str(new / "Contents" / "MacOS" / APP_NAME), str(tmp / "launcher.c")]):
            say(f"[WARN] Could not build {APP_NAME}.app. The satellite runs without it.")
            return False
        iconed = False
        if icon is not None:
            (tmp / "icon.png").write_bytes(icon_png(icon.read_bytes()))
            iconed = _ran(["sips", "-s", "format", "icns", str(tmp / "icon.png"),
                           "--out", str(new / "Contents" / "Resources" / "AppIcon.icns")])
        if not iconed:
            say(f"[WARN] {APP_NAME}.app has no icon.")
        if not _ran(["codesign", "--force", "--sign", "-", "--identifier", LABEL, str(new)]):
            say(f"[WARN] Could not sign {APP_NAME}.app. The satellite runs without it.")
            return False
        had = app.exists()
        if had:
            shutil.rmtree(app)
        shutil.move(str(new), str(app))
    stamp_file.write_text(want + "\n")
    if had:
        for service in ("Accessibility", "ScreenCapture"):
            _ran(["tccutil", "reset", service, LABEL])
        say(f"[INFO] {APP_NAME}.app changed, so macOS asks for its permissions again.")
    _ran([LSREGISTER, "-f", str(app)])
    say(f"[OK] {APP_NAME}.app built")
    return True


if __name__ == "__main__":
    install = Path(sys.argv[1])
    sys.exit(0 if build(install, install / "satellite") else 1)
