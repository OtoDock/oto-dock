import { useEffect, useState, useCallback, useSyncExternalStore, type ReactNode } from 'react'
import { callNative } from '../lib/nativeBridge'
import { onBootDone } from '../lib/device/boot'
import { type Theme, ThemeContext } from './useTheme'

function getSystemTheme(): 'light' | 'dark' {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function subscribeSystemTheme(notify: () => void): () => void {
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  mq.addEventListener('change', notify)
  return () => mq.removeEventListener('change', notify)
}

/** The page colour the tokens give the current theme (`surface.page`), read
 *  from the stylesheet so the app's bars and the browser's own colour follow
 *  the design tokens without a copy of their values here. */
function pageColour(): string {
  return getComputedStyle(document.documentElement).getPropertyValue('--ot-surface-page').trim()
}

function applyTheme(resolved: 'light' | 'dark') {
  document.documentElement.classList.toggle('dark', resolved === 'dark')
  const page = pageColour()
  if (!page) return
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', page)
  // The strips behind the bars stay on the boot field until the boot screen
  // leaves, then cross-fade with it.
  onBootDone(() => { void applyNativeTheme(resolved, page) })
}

/** Sync the Android bars and the container behind them to the web theme. */
async function applyNativeTheme(resolved: 'light' | 'dark', page: string) {
  // The app paints the strips behind the bars, the WebView's own background
  // and both bars' icons from this colour.
  callNative('setContainerColor', page)

  // Apps older than 1.9.0 set their status bar from the page alone.
  try {
    const { StatusBar, Style } = await import('@capacitor/status-bar')
    await StatusBar.setBackgroundColor({ color: page })
    await StatusBar.setStyle({ style: resolved === 'dark' ? Style.Dark : Style.Light })
  } catch { /* not on native platform or plugin unavailable */ }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => {
    const stored = localStorage.getItem('theme')
    return (stored === 'light' || stored === 'dark' || stored === 'system') ? stored : 'system'
  })
  // The system's preference, followed live while the theme is 'system'.
  const systemTheme = useSyncExternalStore(subscribeSystemTheme, getSystemTheme, getSystemTheme)
  const resolvedTheme = theme === 'system' ? systemTheme : theme

  const setTheme = useCallback((t: Theme) => {
    localStorage.setItem('theme', t)
    setThemeState(t)
  }, [])

  // Apply the .dark class (and the native chrome) whenever it changes.
  useEffect(() => { applyTheme(resolvedTheme) }, [resolvedTheme])

  return (
    <ThemeContext.Provider value={{ theme, setTheme, resolvedTheme }}>
      {children}
    </ThemeContext.Provider>
  )
}
