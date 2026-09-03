# Satellite `bin/` — bundled assets

This directory holds assets that ship inside the satellite tarball.

## `otodock.ico` — tray + Add/Remove-Programs icon (Windows)

Multi-resolution OtoDock logo icon (16 / 32 / 48 / 256 px), generated from
the dashboard brand logo (`dashboard/public/logo.png`). Used by:

- the Windows system-tray icon (`satellite/host/tray.py`), and
- the Add/Remove-Programs `DisplayIcon` written by `install.ps1`.

Regenerate with Pillow if the brand logo changes:

```bash
python -c "from PIL import Image; \
  Image.open('dashboard/public/logo.png').convert('RGBA').save(
    'satellite/bin/otodock.ico', sizes=[(16,16),(32,32),(48,48),(256,256)])"
```

## Service manager

None bundled. The satellite runs as a **per-user service** with no extra
binaries: a systemd *user* unit on Linux, a launchd LaunchAgent on macOS,
and a per-user logon **Scheduled Task** on Windows (registered by
`install.ps1`).
