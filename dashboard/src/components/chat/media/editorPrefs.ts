// The editor's own settings the dashboard writes before each load of the
// editor frame (`CollaboraFrame`): the one place that knows their names.
// Collabora's client keeps its settings in the page origin's storage
// (`prefs.get` reads its cache, the ui defaults, the host's settings, then
// `localStorage` under the bare key; its `global.js`, 26.04), and its server
// drops these keys from the address's `ui_defaults`. A Collabora bump
// re-checks both names (VERSIONS.md, the Collabora row).

/** The editor's dark chrome: written at every load, so the editor follows
 * the dashboard's theme, not the device's. */
export const EDITOR_DARK_THEME_KEY = 'darkTheme'

/** The page under the dark chrome: written white only when absent, so a
 * person who inverted it in the editor (View, Invert Background) keeps it. */
export const EDITOR_DARK_PAGE_KEY = 'darkBackgroundForTheme.dark'

/** Seed the editor's theme and page before a frame of `frameUrl` loads.
 * Only an editor on the page's own origin reads this storage (the usual
 * install, and both apps); one on its own address keeps its own. */
export function seedEditorPrefs(frameUrl: string, dark: boolean): void {
  let origin: string
  try {
    origin = new URL(frameUrl, window.location.href).origin
  } catch {
    return
  }
  if (origin !== window.location.origin) return
  try {
    localStorage.setItem(EDITOR_DARK_THEME_KEY, dark ? 'true' : 'false')
    if (localStorage.getItem(EDITOR_DARK_PAGE_KEY) === null) localStorage.setItem(EDITOR_DARK_PAGE_KEY, 'false')
  } catch {
    // Storage off: the editor then decides by itself, as before.
  }
}
