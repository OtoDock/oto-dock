import React from 'react'
import ReactDOM from 'react-dom/client'
import { QueryClientProvider } from '@tanstack/react-query'
import { queryClient } from './lib/queryClient'
import { AuthProvider } from './contexts/AuthContext'
import { ThemeProvider } from './contexts/ThemeContext'
import ErrorBoundary from './components/ErrorBoundary'
import App from './App'
import { fitViewportToDevice } from './lib/device/viewport'
import './index.css'

fitViewportToDevice()

// Stale-chunk recovery: a dashboard rebuild regenerates content-hashed chunk
// names and deletes the old ones, so a tab opened before the rebuild 404s on
// its next lazy import ("Failed to fetch dynamically imported module").
// Vite fires `vite:preloadError` in exactly that case — reload once to pick
// up the new index.html (sessionStorage-guarded so a genuine outage can't
// cause a reload loop).
window.addEventListener('vite:preloadError', (event) => {
  const KEY = 'oto-chunk-reload-at'
  const last = Number(sessionStorage.getItem(KEY) || 0)
  if (Date.now() - last < 10_000) return // just reloaded — let the error surface
  sessionStorage.setItem(KEY, String(Date.now()))
  event.preventDefault() // suppress the doomed import error
  window.location.reload()
})

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <QueryClientProvider client={queryClient}>
        <ThemeProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </ThemeProvider>
      </QueryClientProvider>
    </ErrorBoundary>
  </React.StrictMode>,
)
