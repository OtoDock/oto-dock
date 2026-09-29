import { useEffect, useState } from 'react'
import { fetchAppLogs, type PinnedApp } from '../../api/apps'
import type { AppServerState } from '../../lib/status/appServer'

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
  stopped: 'stopped (starts on the next request)',
  backoff: 'failed — waiting before the next start',
  quota_full: 'stopped — the storage quota is full',
  static: 'no server (a client-only app)',
  unapproved: 'waiting for approval',
  secrets: 'waiting for a secret to be set (Settings in the menu)',
  failed: 'failed to start',
}

export default function AppLogsPanel({ app, onClose }: Props) {
  const [log, setLog] = useState<string>('')
  const [server, setServer] = useState<string>('')
  const [error, setError] = useState<string>('')
  const [loading, setLoading] = useState(false)

  const load = () => {
    setLoading(true)
    fetchAppLogs(app.id, 400)
      .then((r) => { setLog(r.log); setServer(r.error ? `${r.server}: ${r.error}` : r.server); setError('') })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }
  useEffect(() => { load() }, [app.id]) // eslint-disable-line react-hooks/exhaustive-deps

  const state = server.split(':')[0]
  return (
    <div className="mx-3 mt-2 rounded-xl border border-p-border-light bg-p-surface text-xs" data-testid="app-logs-panel">
      <div className="flex items-center gap-2 border-b border-p-border-light/60 px-3 py-2">
        <span className="font-medium text-p-text">Logs of “{app.title || app.slug}”</span>
        <span className="text-p-text-light">
          server {STATE_WORDS[state as AppServerState] || server || '…'}{server.includes(':') ? ` (${server.split(':').slice(1).join(':').trim()})` : ''}
        </span>
        <span className="ml-auto flex items-center gap-1.5">
          <button
            onClick={load}
            disabled={loading}
            className="rounded-md border border-p-border-light px-2 py-0.5 text-p-text-secondary transition-colors hover:bg-p-surface-hover disabled:opacity-60"
          >
            Refresh
          </button>
          <button
            onClick={onClose}
            aria-label="Close logs"
            className="rounded-md border border-p-border-light px-2 py-0.5 text-p-text-secondary transition-colors hover:bg-p-surface-hover"
          >
            ✕
          </button>
        </span>
      </div>
      {error ? (
        <p className="px-3 py-2 text-red-500">{error}</p>
      ) : (
        <pre className="max-h-64 overflow-auto px-3 py-2 font-mono text-[10px] leading-snug text-p-text-secondary">
          {log || (loading ? 'Loading…' : 'No log lines yet — the server has not started since the platform did.')}
        </pre>
      )}
    </div>
  )
}
