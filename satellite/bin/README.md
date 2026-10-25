# Satellite `bin/` — bundled assets

This directory holds assets that ship inside the satellite tarball.

## `otodock.ico` — tray + Add/Remove-Programs icon (Windows)

The OtoDock mark at 16, 32, 48 and 256 px (the small optical version at
16 and 32, the flat mark above), white on the mark's field in a rounded
square, so it reads on a light and a dark taskbar. Used by:

- the Windows system-tray icon (`satellite/host/tray.py`), which draws its
  status dot over the largest frame, and
- the Add/Remove-Programs `DisplayIcon` written by `install.ps1` and the
  Start menu shortcut.

It is rendered from the master mark (`dashboard/design/brand/otodock-mark.svg`)
with the dashboard's other brand files; never edit it by hand:

```bash
cd dashboard && npm run brand:export
```

## Service manager

None bundled. The satellite runs as a **per-user service** with no extra
binaries: a systemd *user* unit on Linux, a launchd LaunchAgent on macOS,
and a per-user logon **Scheduled Task** on Windows (registered by
`install.ps1`).
