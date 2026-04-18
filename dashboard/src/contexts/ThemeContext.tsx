import { createContext, useContext, useEffect, useState, useCallback, type ReactNode } from 'react'

type Theme = 'system' | 'light' | 'dark'

interface ThemeContextValue {
  theme: Theme
  setTheme: (t: Theme) => void
  resolvedTheme: 'light' | 'dark'
}

const ThemeContext = createContext<ThemeContextValue>({
  theme: 'system',
  setTheme: () => {},
  resolvedTheme: 'light',
})

function getSystemTheme(): 'light' | 'dark' {
  return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
}

// Native Android colors matching CSS --p-bg values
const NATIVE_BG = { light: '#FAF9F9', dark: '#121620' }

function applyTheme(resolved: 'light' | 'dark') {
  document.documentElement.classList.toggle('dark', resolved === 'dark')
  applyNativeTheme(resolved)
}

/** Sync Android status bar and container background to match the web theme */
async function applyNativeTheme(resolved: 'light' | 'dark') {
  const bg = NATIVE_BG[resolved]

  // Update Android WebView container background (safe-area behind status/nav bars)
  try {
    const android = (window as any).Android
    if (android?.setContainerColor) {
      android.setContainerColor(bg)
    }
  } catch { /* not on Android */ }

  // Update Android status bar color and icon style
  // Style.Dark = light text (for dark bg), Style.Light = dark text (for light bg)
  try {
    const { StatusBar, Style } = await import('@capacitor/status-bar')
    await StatusBar.setBackgroundColor({ color: bg })
    await StatusBar.setStyle({ style: resolved === 'dark' ? Style.Dark : Style.Light })
  } catch { /* not on native platform or plugin unavailable */ }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(() => {
    const stored = localStorage.getItem('theme')
    return (stored === 'light' || stored === 'dark' || stored === 'system') ? stored : 'system'
  })
  const [resolvedTheme, setResolvedTheme] = useState<'light' | 'dark'>(() => {
    const stored = localStorage.getItem('theme')
    if (stored === 'light') return 'light'
    if (stored === 'dark') return 'dark'
    return getSystemTheme()
  })

  const setTheme = useCallback((t: Theme) => {
    localStorage.setItem('theme', t)
    setThemeState(t)
  }, [])

  // Resolve theme and apply .dark class
  useEffect(() => {
    const resolved = theme === 'system' ? getSystemTheme() : theme
    setResolvedTheme(resolved)
    applyTheme(resolved)
  }, [theme])

  // Listen for system theme changes when in 'system' mode
  useEffect(() => {
    if (theme !== 'system') return
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const handler = () => {
      const resolved = getSystemTheme()
      setResolvedTheme(resolved)
      applyTheme(resolved)
    }
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [theme])

  return (
    <ThemeContext.Provider value={{ theme, setTheme, resolvedTheme }}>
      {children}
    </ThemeContext.Provider>
  )
}

export function useTheme() {
  return useContext(ThemeContext)
}
