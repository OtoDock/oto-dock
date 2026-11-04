// The apps' bars (the strips behind the status and gesture bars, the
// WebView's own background, both bars' icons) follow the web theme's page
// colour, or the colour a page paints its own bars with while it shows.
import { callNative } from '../nativeBridge'

/** The page colour the tokens give the current theme (`surface.page`), read
 *  from the stylesheet so the app's bars and the browser's own colour follow
 *  the design tokens without a copy of their values here. */
export function pageColour(): string {
  return getComputedStyle(document.documentElement).getPropertyValue('--ot-surface-page').trim()
}

// The theme last applied, re-sent when a page's bars override ends.
let lastResolved: 'light' | 'dark' = 'light'
// A page that paints its own bars (the agents map keeps the dark theme's
// bars in the light theme) sets this while it shows; the apps then paint
// from it instead of the theme's page colour.
let barsOverride: string | null = null

/** Sync the apps' bars and the container behind them to the web theme. */
export async function applyNativeTheme(resolved: 'light' | 'dark', page: string) {
  lastResolved = resolved
  // The app paints the strips behind the bars, the WebView's own background
  // and both bars' icons from this colour.
  const bars = barsOverride ?? page
  callNative('setContainerColor', bars)

  // Apps older than 1.9.0 set their status bar from the page alone.
  try {
    const { StatusBar, Style } = await import('@capacitor/status-bar')
    await StatusBar.setBackgroundColor({ color: bars })
    await StatusBar.setStyle({ style: barsOverride || resolved === 'dark' ? Style.Dark : Style.Light })
  } catch { /* not on native platform or plugin unavailable */ }
}

/** A page's bars take `colour` until `endBarsOverride`; the theme's come back then. */
export function startBarsOverride(colour: string) {
  barsOverride = colour
  void applyNativeTheme(lastResolved, pageColour())
}

export function endBarsOverride() {
  barsOverride = null
  void applyNativeTheme(lastResolved, pageColour())
}
