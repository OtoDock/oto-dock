import { useEffect, useState } from 'react'
import { fetchAppLogs, type PinnedApp } from '../../api/apps'
import type { AppServerState } from '../../lib/status/appServer'
import Button from '../ui/Button'
import { CARD } from '../ui/classes'

/**
 * The server log of a folder app (APPS.md "The supervisor"): the last
 * lines the supervisor wrote, with the server's state, for the owner or
 * an editor who opened Logs from the menu. Refreshes on request; never
 * streams (the log file is the source, the runtime is not).
 */

interface Props {
  app: PinnedApp
  onClose: () => void
}

// A label per server state (lib/status/appServer.ts). The logs route serves
// the supervisor's own words; the shim's `failed` never reaches it.
const STATE_WORDS: Record<AppServerState, string> = {
  up: 'running',
  starting: 'starting',
  stopped: 'stopped, starts on the next request',
  backoff: 'failed, waiting to start again',
  quota_full: 'stopped',
  static: 'no server, a client-only app',
  unapproved: 'waiting for approval',
  secrets: 'waiting for a secret to be set. Open Settings in the menu',
  failed: 'failed to start',
}

export default function AppLogsPanel({ app, onClose }: Props) {
  const [log, setLog] = useState<string>('')
  const [server, setServer] = useState<string>('')
  const [error, setError] = useState<string>('')
  // Each request is the app and a refresh count; the panel loads until the
  // one it shows has settled.
  const [refreshes, setRefreshes] = useState(0)
  const request = `${app.id}#${refreshes}`
  const [settled, setSettled] = useState<string | null>(null)
  const loading = settled !== request
  useEffect(() => {
    let alive = true
    fetchAppLogs(app.id, 400)
      .then((r) => {
        if (!alive) return
        setLog(r.log); setServer(r.error ? `${r.server}: ${r.error}` : r.server); setError('')
      })
      .catch((e: Error) => { if (alive) setError(e.message) })
      .finally(() => { if (alive) setSettled(request) })
    return () => { alive = false }
  }, [app.id, request])

  const state = server.split(':')[0]
  return (
    <div className={`mx-3 mt-2 text-xs ${CARD}`} data-testid="app-logs-panel">
      <div className="flex items-center gap-2 border-b border-subtle px-3 py-2">
        <span className="font-medium text-primary">Logs of “{app.title || app.slug}”</span>
        <span className="text-tertiary">
          server {STATE_WORDS[state as AppServerState] || server || '…'}{server.includes(':') ? `: ${server.split(':').slice(1).join(':').trim()}` : ''}
        </span>
        <span className="ml-auto flex items-center gap-1.5">
          <Button size="xs" onClick={() => setRefreshes((n) => n + 1)} disabled={loading}>
            Refresh
          </Button>
          <Button size="xs" onClick={onClose} aria-label="Close logs">
            ✕
          </Button>
        </span>
      </div>
      {error ? (
        <p className="px-3 py-2 text-status-danger-fg">{error}</p>
      ) : (
        <pre className="max-h-64 overflow-auto rounded-b-card bg-surface-sunken px-3 py-2 font-mono text-3xs leading-snug text-secondary">
          {log || (loading ? 'Loading…' : 'No log lines yet. The app’s server has not started since OtoDock did.')}
        </pre>
      )}
    </div>
  )
}
