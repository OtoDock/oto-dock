// Foreground build check for pages without a dashboard socket.
//
// The socket (chat pages) learns the server's build on every reconnect and
// every pong. A tab parked on Agents / Settings / Admin, or the app coming
// back from the background, has no socket — so on `visibilitychange` →
// visible and on `otodock:force-health-check` (the app's onResume) this
// asks /health for the build the server serves now and hands it to the
// same comparison. Throttled: a burst of both events costs one request.

import { useEffect } from 'react'
import { noteServerBuild } from '../lib/buildId'

const MIN_INTERVAL_MS = 20_000

export function useBuildWatch(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return
    let last = 0
    let cancelled = false
    const check = () => {
      if (document.visibilityState !== 'visible') return
      const now = Date.now()
      if (now - last < MIN_INTERVAL_MS) return
      last = now
      fetch('/health', { cache: 'no-store', credentials: 'same-origin' })
        .then((r) => (r.ok ? r.json() : null))
        .then((j) => { if (!cancelled && j) noteServerBuild(j.build) })
        .catch(() => { /* offline or restarting — the next signal retries */ })
    }
    document.addEventListener('visibilitychange', check)
    window.addEventListener('otodock:force-health-check', check)
    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', check)
      window.removeEventListener('otodock:force-health-check', check)
    }
  }, [enabled])
}
