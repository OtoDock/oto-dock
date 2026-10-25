// The colour theme as the page reads it: the context object and `useTheme`.
// ThemeProvider (ThemeContext.tsx) resolves and applies it.

import { createContext, useContext } from 'react'

export type Theme = 'system' | 'light' | 'dark'

export interface ThemeContextValue {
  theme: Theme
  setTheme: (t: Theme) => void
  resolvedTheme: 'light' | 'dark'
}

export const ThemeContext = createContext<ThemeContextValue>({
  theme: 'system',
  setTheme: () => {},
  resolvedTheme: 'light',
})

export function useTheme() {
  return useContext(ThemeContext)
}
