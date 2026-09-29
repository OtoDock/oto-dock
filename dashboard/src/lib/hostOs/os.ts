/**
 * The host OS words — mirrored from the proxy's authority `core/host_os.py`
 * (`tests/core/test_host_os.py` binds this file to it; edit both).
 *
 * Three vocabularies name one thing. A satellite reports its OS on the wire
 * as `platform.system().lower()` (`capabilities.os`: `SATELLITE_OS`); the
 * pairing modal offers three install commands keyed by the bootstrap
 * route's `?os=` words (`BOOTSTRAP_OS` — the install FLAVOUR: `macos` is
 * the bash bootstrap, never a wire word); a machine's display server is
 * one of `DISPLAY_SERVER`. The dashboard branches on none of them — it
 * labels the modal's tabs and types the machine row.
 */

/** The wire word a satellite reports (`capabilities.os`). */
export const SATELLITE_OS = ['linux', 'darwin', 'windows'] as const
export type SatelliteOs = (typeof SATELLITE_OS)[number]

/** The bootstrap route's `?os=` words — the pairing modal's command keys, in tab order. */
export const BOOTSTRAP_OS = ['linux', 'macos', 'windows'] as const
export type BootstrapOs = (typeof BOOTSTRAP_OS)[number]

/** The display server a satellite reports (`capabilities.display.server`). */
export const DISPLAY_SERVER = ['x11', 'wayland', 'quartz', 'windows', 'none'] as const
export type DisplayServer = (typeof DISPLAY_SERVER)[number]

/** Best-effort guess of the viewer's own OS, for the modal's initial tab. */
export function detectBootstrapOs(): BootstrapOs {
  if (typeof navigator === 'undefined') return 'linux'
  const ua = navigator.userAgent.toLowerCase()
  if (ua.includes('win')) return 'windows'
  if (ua.includes('mac')) return 'macos'
  return 'linux'
}
