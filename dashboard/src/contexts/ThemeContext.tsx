import { useEffect, useState, useCallback, useSyncExternalStore, type ReactNode } from 'react'
import { onBootDone } from '../lib/device/boot'
import { applyNativeTheme, pageColour } from '../lib/device/nativeBars'
import { type Theme, ThemeContext } from './useTheme'

function getSystemTheme(): 'light' | 'dark' {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

function subscribeSystemTheme(notify: () => void): () => void {
  const mq = window.matchMedia('(prefers-color-scheme: dark)')
  mq.addEventListener('change', notify)
  return () => mq.removeEventListener('change', notify)
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
